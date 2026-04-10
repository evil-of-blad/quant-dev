"""
资金费率套利引擎

原理:
  当永续合约资金费率为正时（多方付给空方）:
    1. 买入现货 (做多)
    2. 做空等量永续合约
    3. 多空对冲，价格涨跌不亏
    4. 每8小时收取资金费率

  当费率转负或低于阈值时:
    1. 卖出现货
    2. 平掉空单
    3. 利润 = 累计收取的资金费率 - 手续费

运行模式:
  - 每小时检查一次费率
  - 费率 > 阈值 → 开仓（如果没有仓位）
  - 费率 < 退出阈值 → 平仓（如果有仓位）
  - 定时播报收益
"""
import asyncio
from datetime import datetime
from loguru import logger
from core.exchange import AsyncExchange
from core.notifier import TelegramNotifier


class FundingArbTrader:

    def __init__(self, config: dict):
        self.config = config
        self.exchange = AsyncExchange(config)
        self.notifier = TelegramNotifier(config)

        arb_cfg = config.get("funding_arb", {})
        self.symbols: list[str] = arb_cfg.get("symbols", ["ETH/USDT:USDT"])
        self.capital: float = arb_cfg.get("capital", 800.0)
        self.per_symbol_pct: float = arb_cfg.get("per_symbol_pct", 0.3)
        self.entry_rate: float = arb_cfg.get("entry_rate", 0.0005)     # 费率 > 0.05% 才开仓
        self.exit_rate: float = arb_cfg.get("exit_rate", 0.0001)       # 费率 < 0.01% 平仓
        self.leverage: int = arb_cfg.get("leverage", 1)                # 合约端杠杆（套利建议1x）
        self.check_interval: int = arb_cfg.get("check_interval", 3600) # 检查间隔秒数

        self._running = False
        # symbol → {spot_amount, swap_amount, entry_rate, total_earned, opened_at}
        self._positions: dict[str, dict] = {}
        self._total_earned: float = 0.0

    async def start(self):
        await self.exchange.init()

        # 套利用1x杠杆
        for symbol in self.symbols:
            await self.exchange.set_leverage(symbol, self.leverage, "isolated")

        logger.info(
            f"[费率套利] 启动 | 标的:{self.symbols} | 资金:{self.capital} USDT "
            f"| 开仓阈值:{self.entry_rate:.4%} | 退出阈值:{self.exit_rate:.4%}"
        )
        await self.notifier.send(
            f"💰 <b>费率套利启动</b>\n"
            f"标的: <code>{', '.join(self.symbols)}</code>\n"
            f"资金: <code>{self.capital:.0f} USDT</code>\n"
            f"开仓阈值: 费率 ≥ {self.entry_rate:.4%}\n"
            f"退出阈值: 费率 ≤ {self.exit_rate:.4%}"
        )

        self._running = True
        await self._main_loop()

    async def stop(self):
        self._running = False
        await self.exchange.close()
        logger.info("[费率套利] 已停止")

    async def _main_loop(self):
        tick_count = 0
        while self._running:
            try:
                await self._tick()
                tick_count += 1
                # 每24小时播报一次
                if tick_count % 24 == 0:
                    await self._report()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[费率套利] 异常: " + str(e), exc_info=True)
                await self.notifier.notify_error(f"费率套利: {e}")
            await asyncio.sleep(self.check_interval)

    async def _tick(self):
        for symbol in self.symbols:
            try:
                await self._process_symbol(symbol)
            except Exception as e:
                logger.error(f"[费率套利] {symbol} 处理失败: " + str(e))

    async def _process_symbol(self, symbol: str):
        # 获取当前资金费率
        funding = await self._fetch_funding_rate(symbol)
        if funding is None:
            return

        rate = funding["rate"]
        pos = self._positions.get(symbol)

        # 现货 symbol（去掉 :USDT 后缀）
        spot_symbol = symbol.replace(":USDT", "")

        if pos:
            # 已有仓位：检查是否该退出
            if rate < self.exit_rate:
                await self._close_arb(symbol, spot_symbol, pos, rate)
            else:
                # 记录本次费率收益
                estimated_earn = rate * pos["swap_amount"] * pos.get("entry_price", 0)
                pos["total_earned"] += estimated_earn
                self._total_earned += estimated_earn
                logger.info(
                    f"[费率套利] {symbol} 持仓中 | 当前费率:{rate:.4%} | "
                    f"本次收益≈{estimated_earn:.4f} | 累计:{pos['total_earned']:.4f}"
                )
        else:
            # 无仓位：检查是否该开仓
            if rate >= self.entry_rate:
                await self._open_arb(symbol, spot_symbol, rate)

    async def _fetch_funding_rate(self, symbol: str) -> dict:
        """获取资金费率"""
        try:
            fr = await self.exchange.client.fetch_funding_rate(symbol)
            rate = fr.get("fundingRate", 0) or 0
            next_time = fr.get("fundingDatetime", "")
            logger.debug(f"[费率套利] {symbol} 费率:{rate:.6%} 下次结算:{next_time}")
            return {"rate": rate, "next_time": next_time}
        except Exception as e:
            logger.warning(f"[费率套利] 获取费率失败 {symbol}: " + str(e))
            return None

    async def _open_arb(self, symbol: str, spot_symbol: str, rate: float):
        """开套利仓位：买现货 + 空合约"""
        per_capital = self.capital * self.per_symbol_pct

        try:
            # 获取现价
            ticker = await self.exchange.fetch_ticker(symbol)
            price = ticker["last"]

            # 计算数量
            amount = per_capital / price

            # 精度处理
            markets = self.exchange.public.markets
            if symbol in markets:
                precision = markets[symbol].get("precision", {}).get("amount", 0.001)
                if isinstance(precision, int):
                    factor = 10 ** precision
                    amount = int(amount * factor) / factor
                else:
                    amount = int(amount / precision) * precision

            if amount <= 0:
                return

            # 1. 买入现货
            logger.info(f"[费率套利] {spot_symbol} 买入现货 {amount:.6f} @ {price:.4f}")
            spot_order = await self.exchange.client.create_market_order(
                spot_symbol, "buy", amount,
                params={"tdMode": "cash"}
            )
            spot_filled = spot_order.get("average", price) or price

            # 2. 做空合约
            logger.info(f"[费率套利] {symbol} 做空合约 {amount:.6f} @ {price:.4f}")
            swap_order = await self.exchange.create_market_order(symbol, "sell", amount)
            swap_filled = swap_order.get("average", price) or price

            self._positions[symbol] = {
                "spot_amount": amount,
                "swap_amount": amount,
                "entry_price": price,
                "spot_entry": spot_filled,
                "swap_entry": swap_filled,
                "entry_rate": rate,
                "total_earned": 0.0,
                "opened_at": datetime.utcnow().strftime("%m-%d %H:%M"),
            }

            margin = per_capital
            logger.success(
                f"[费率套利] 开仓完成 {symbol} | 费率:{rate:.4%} | "
                f"数量:{amount:.6f} | 资金:{margin:.2f} USDT"
            )
            await self.notifier.send(
                f"💰 <b>费率套利开仓</b>\n"
                f"标的: <code>{symbol}</code>\n"
                f"费率: <code>{rate:.4%}</code> (年化 {rate*3*365*100:.1f}%)\n"
                f"数量: <code>{amount:.6f}</code>\n"
                f"现货买入: <code>{spot_filled:.4f}</code>\n"
                f"合约做空: <code>{swap_filled:.4f}</code>\n"
                f"占用资金: <code>{margin:.2f} USDT</code>"
            )

        except Exception as e:
            logger.error("[费率套利] 开仓失败: " + str(e))
            await self.notifier.notify_error(f"费率套利开仓失败 {symbol}: {e}")

    async def _close_arb(self, symbol: str, spot_symbol: str, pos: dict, rate: float):
        """平套利仓位：卖现货 + 平空合约"""
        try:
            # 1. 卖出现货
            logger.info(f"[费率套利] {spot_symbol} 卖出现货 {pos['spot_amount']:.6f}")
            await self.exchange.client.create_market_order(
                spot_symbol, "sell", pos["spot_amount"],
                params={"tdMode": "cash"}
            )

            # 2. 平掉合约空单
            logger.info(f"[费率套利] {symbol} 平空合约 {pos['swap_amount']:.6f}")
            await self.exchange.create_market_order(symbol, "buy", pos["swap_amount"])

            earned = pos["total_earned"]
            self._positions.pop(symbol, None)

            logger.success(
                f"[费率套利] 平仓完成 {symbol} | 累计费率收益:{earned:.4f} USDT | "
                f"退出原因: 费率降至 {rate:.4%}"
            )
            await self.notifier.send(
                f"💰 <b>费率套利平仓</b>\n"
                f"标的: <code>{symbol}</code>\n"
                f"累计费率收益: <code>{earned:+.4f} USDT</code>\n"
                f"退出原因: 费率降至 {rate:.4%}\n"
                f"持仓时长: 自 {pos['opened_at']}"
            )

        except Exception as e:
            logger.error("[费率套利] 平仓失败: " + str(e))
            await self.notifier.notify_error(f"费率套利平仓失败 {symbol}: {e}")

    async def _report(self):
        """定时播报"""
        if not self._positions:
            await self.notifier.send(
                f"💰 <b>费率套利日报</b>\n"
                f"当前无持仓\n"
                f"累计收益: <code>{self._total_earned:+.4f} USDT</code>"
            )
            return

        lines = ""
        for sym, pos in self._positions.items():
            fr = await self._fetch_funding_rate(sym)
            curr_rate = fr["rate"] if fr else 0
            lines += (
                f"\n📌 <b>{sym}</b>"
                f"\n   数量: {pos['swap_amount']:.6f}"
                f"\n   当前费率: {curr_rate:.4%}"
                f"\n   累计收益: {pos['total_earned']:+.4f} USDT"
                f"\n   开仓时间: {pos['opened_at']}\n"
            )

        await self.notifier.send(
            f"💰 <b>费率套利日报</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"持仓数: {len(self._positions)}\n"
            f"总累计收益: <code>{self._total_earned:+.4f} USDT</code>\n"
            f"{lines}"
        )
