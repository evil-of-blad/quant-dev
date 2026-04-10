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

        # 套利用1x杠杆，每个标的都校验
        self._lev_ok: dict[str, bool] = {}
        for symbol in self.symbols:
            await self.exchange.set_margin_mode(symbol, "isolated")
            ok = await self.exchange.set_leverage(symbol, self.leverage, "isolated")
            self._lev_ok[symbol] = ok
            if not ok:
                logger.warning(f"[费率套利] {symbol} 杠杆配置异常，将跳过该标的")

        # ★ 启动时仓位同步：从交易所拉取真实持仓，重建内存状态
        await self._reconcile_positions()

        logger.info(
            f"[费率套利] 启动 | 标的:{self.symbols} | 资金:{self.capital} USDT "
            f"| 开仓阈值:{self.entry_rate:.4%} | 退出阈值:{self.exit_rate:.4%}"
        )
        await self.notifier.send(
            f"💰 <b>费率套利启动</b>\n"
            f"标的: <code>{', '.join(self.symbols)}</code>\n"
            f"资金: <code>{self.capital:.0f} USDT</code>\n"
            f"开仓阈值: 费率 ≥ {self.entry_rate:.4%}\n"
            f"退出阈值: 费率 ≤ {self.exit_rate:.4%}\n"
            f"已恢复持仓: <code>{len(self._positions)}</code> 个"
        )

        self._running = True
        await self._main_loop()

    async def _reconcile_positions(self):
        """
        启动时同步交易所真实持仓到内存。

        逻辑:
        1. 拉取所有合约持仓和现货余额
        2. 对每个目标 symbol:
           - 检查合约空单数量
           - 检查对应现货余额
           - 如果两边数量匹配 → 重建套利持仓
           - 如果只有一边 → 记录孤立仓位，告警让用户处理
        """
        try:
            balance = await self.exchange.fetch_balance()
            positions = await self.exchange.client.fetch_positions(self.symbols)
        except Exception as e:
            logger.warning(f"[费率套利] 拉取持仓失败，跳过同步: {e}")
            return

        warnings = []
        recovered = []

        for symbol in self.symbols:
            spot_symbol = symbol.replace(":USDT", "")
            base_coin = symbol.split("/")[0]  # ETH/USDT:USDT → ETH

            # 合约空单数量
            swap_short = 0.0
            swap_entry = 0.0
            for p in positions:
                if p.get("symbol") == symbol and p.get("side") == "short":
                    swap_short = abs(float(p.get("contracts", 0) or 0))
                    swap_entry = float(p.get("entryPrice", 0) or 0)
                    break

            # 现货余额
            spot_balance = balance.get(base_coin, {}).get("total", 0) or 0

            if swap_short > 1e-9 and spot_balance > 1e-9:
                # 双边都有，按较小值重建
                amount = min(swap_short, spot_balance)
                self._positions[symbol] = {
                    "spot_amount": amount,
                    "swap_amount": amount,
                    "entry_price": swap_entry or 0,
                    "spot_entry": 0,
                    "swap_entry": swap_entry or 0,
                    "entry_rate": 0,
                    "total_earned": 0.0,
                    "opened_at": "(启动恢复)",
                }
                recovered.append(f"{symbol} 数量={amount:.6f}")
                logger.info(
                    f"[费率套利] 恢复持仓: {symbol} | 现货:{spot_balance:.6f} 空单:{swap_short:.6f}"
                )

                # 检查不平衡
                if abs(swap_short - spot_balance) > max(swap_short, spot_balance) * 0.05:
                    warnings.append(
                        f"{symbol} 现货({spot_balance:.6f})与空单({swap_short:.6f})不平衡"
                    )

            elif swap_short > 1e-9 and spot_balance < 1e-9:
                # 只有合约空单，没有现货
                warnings.append(f"{symbol} 只有合约空单 {swap_short:.6f}，没有对应现货")
                logger.warning(f"[费率套利] 孤立空单: {symbol} {swap_short}")

            elif swap_short < 1e-9 and spot_balance > 1e-9:
                # 只有现货，没有合约空单
                warnings.append(f"{base_coin} 只有现货 {spot_balance:.6f}，没有对冲空单")
                logger.warning(f"[费率套利] 孤立现货: {base_coin} {spot_balance}")

        if recovered:
            logger.info(f"[费率套利] 已恢复 {len(recovered)} 个套利仓位")
            for r in recovered:
                logger.info(f"  • {r}")

        if warnings:
            warn_msg = "\n".join(f"⚠️ {w}" for w in warnings)
            logger.warning(f"[费率套利] 检测到孤立仓位：\n{warn_msg}")
            await self.notifier.send(
                f"⚠️ <b>启动检测到不平衡持仓</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"{warn_msg}\n\n"
                f"<i>请手动平衡或忽略，机器人不会主动处理孤立仓位</i>"
            )

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
        # 杠杆配置异常的标的直接跳过
        if not self._lev_ok.get(symbol, True):
            return

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
        """
        开套利仓位（原子化）:
        1. 校验杠杆
        2. 先尝试合约空单（容易因杠杆/数量限制失败）
        3. 合约成功后再买现货
        4. 现货失败则立刻平掉合约空单回滚
        """
        per_capital = self.capital * self.per_symbol_pct

        # 下单前校验杠杆
        if not await self.exchange.ensure_leverage(symbol, self.leverage, "isolated"):
            logger.warning(f"[费率套利] {symbol} 杠杆校验失败，跳过本次开仓")
            return

        try:
            ticker = await self.exchange.fetch_ticker(symbol)
            price = ticker["last"]
            amount = self._calc_amount(symbol, per_capital, price)
            if amount <= 0:
                return

            # ---- 1. 先做空合约 ----
            logger.info(f"[费率套利] {symbol} 做空合约 {amount:.6f} @ {price:.4f}")
            try:
                swap_order = await self.exchange.create_market_order(symbol, "sell", amount)
                swap_filled = swap_order.get("average", price) or price
                swap_amount = swap_order.get("filled", amount) or amount
            except Exception as e:
                logger.error(f"[费率套利] 合约空单失败: {e}")
                await self.notifier.notify_error(f"费率套利-合约空单失败 {symbol}: {e}")
                return

            # ---- 2. 再买入现货 ----
            logger.info(f"[费率套利] {spot_symbol} 买入现货 {swap_amount:.6f} @ {price:.4f}")
            try:
                spot_order = await self.exchange.client.create_market_order(
                    spot_symbol, "buy", swap_amount,
                    params={"tdMode": "cash"}
                )
                spot_filled = spot_order.get("average", price) or price
            except Exception as e:
                # ---- 现货失败，回滚合约空单 ----
                logger.error(f"[费率套利] 现货买入失败，回滚合约空单: {e}")
                try:
                    await self.exchange.create_market_order(symbol, "buy", swap_amount)
                    logger.info(f"[费率套利] 合约空单已回滚")
                    await self.notifier.notify_error(
                        f"费率套利-现货失败已回滚 {symbol}: {str(e)[:200]}"
                    )
                except Exception as rb_err:
                    logger.error(f"[费率套利] 回滚失败！需要手动处理: {rb_err}")
                    await self.notifier.notify_error(
                        f"⚠️ 套利回滚失败！请手动平掉 {symbol} 空单！原因: {rb_err}"
                    )
                return

            # ---- 3. 双向都成功，记录仓位 ----
            self._positions[symbol] = {
                "spot_amount": swap_amount,
                "swap_amount": swap_amount,
                "entry_price": price,
                "spot_entry": spot_filled,
                "swap_entry": swap_filled,
                "entry_rate": rate,
                "total_earned": 0.0,
                "opened_at": datetime.utcnow().strftime("%m-%d %H:%M"),
            }

            logger.success(
                f"[费率套利] 开仓完成 {symbol} | 费率:{rate:.4%} | "
                f"数量:{swap_amount:.6f} | 资金:{per_capital:.2f} USDT"
            )
            await self.notifier.send(
                f"💰 <b>费率套利开仓</b>\n"
                f"标的: <code>{symbol}</code>\n"
                f"费率: <code>{rate:.4%}</code> (年化 {rate*3*365*100:.1f}%)\n"
                f"数量: <code>{swap_amount:.6f}</code>\n"
                f"现货买入: <code>{spot_filled:.4f}</code>\n"
                f"合约做空: <code>{swap_filled:.4f}</code>\n"
                f"占用资金: <code>{per_capital:.2f} USDT</code>"
            )

        except Exception as e:
            logger.error("[费率套利] 开仓异常: " + str(e), exc_info=True)
            await self.notifier.notify_error(f"费率套利开仓异常 {symbol}: {e}")

    def _calc_amount(self, symbol: str, capital: float, price: float) -> float:
        """计算开仓量并按精度截断"""
        amount = capital / price
        markets = self.exchange.public.markets
        if symbol in markets:
            m = markets[symbol]
            precision = m.get("precision", {}).get("amount", 0.001)
            min_amount = m.get("limits", {}).get("amount", {}).get("min", 0)

            if isinstance(precision, int):
                factor = 10 ** precision
                amount = int(amount * factor) / factor
            else:
                amount = int(amount / precision) * precision

            if amount < min_amount:
                logger.warning(f"[费率套利] {symbol} 数量 {amount} 小于最小值 {min_amount}")
                return 0.0
        return amount

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
