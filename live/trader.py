"""
实盘交易引擎（合约版）— 异步轮询，支持多空双向，Telegram 通知
"""
import asyncio
from datetime import datetime
from loguru import logger
from core.exchange import AsyncExchange
from core.data_feed import add_indicators
from core.risk import RiskManager
from core.notifier import TelegramNotifier
from core.bot_commands import TelegramBot
from core.allocation import CapitalAllocator
from strategies.base import BaseStrategy


class LiveTrader:
    TIMEFRAME_SECONDS = {
        "1m": 60, "3m": 180, "5m": 300, "15m": 900,
        "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400,
    }

    def __init__(self, config: dict, strategy: BaseStrategy):
        self.config = config
        self.strategy = strategy
        self.exchange = AsyncExchange(config)
        self.risk = RiskManager(config)
        self.notifier = TelegramNotifier(config)
        self.bot = TelegramBot(config, self)
        self.allocator = CapitalAllocator(config)
        self.allocated_capital: float = 0.0

        t_cfg = config["trading"]
        self.symbols: list[str] = t_cfg["symbols"]
        self.timeframe: str = t_cfg["timeframe"]
        self.leverage: int = t_cfg.get("leverage", 1)
        self.margin_mode: str = t_cfg.get("margin_mode", "isolated")
        self.poll_interval: int = self.TIMEFRAME_SECONDS.get(self.timeframe, 14400)

        # 每 N 次 tick 播报一次状态（默认每6次=24h）
        self._status_interval: int = config.get("telegram", {}).get("status_interval", 6)
        self._tick_count: int = 0

        self._running = False
        self._positions: dict[str, dict] = {}

    async def start(self):
        await self.exchange.init()

        for symbol in self.symbols:
            await self.exchange.set_margin_mode(symbol, self.margin_mode, self.leverage)
            await self.exchange.set_leverage(symbol, self.leverage, self.margin_mode)

        self._markets = self.exchange.public.markets if hasattr(self.exchange, 'public') else {}

        # 资金分配
        await self.allocator.init(self.exchange)
        self.allocated_capital = self.allocator.get("bollinger")
        if self.allocated_capital <= 0:
            logger.error("布林带未分配资金，请检查 config.allocation.bollinger_pct")
            return

        # ★ 启动时仓位同步：从交易所拉取真实持仓
        await self._reconcile_positions()

        logger.info(
            f"实盘启动 | 策略:{self.strategy.name} | 标的:{self.symbols} "
            f"| 杠杆:{self.leverage}x | 保证金:{self.margin_mode} | 周期:{self.timeframe} "
            f"| 已恢复持仓:{len(self._positions)}"
        )

        # 启动通知
        balance = await self.exchange.fetch_balance()
        equity = balance.get("USDT", {}).get("total", 0) or 0
        await self.notifier.notify_startup(self.strategy.name, self.symbols, self.leverage, equity)

        self._running = True

        # 启动 Telegram Bot 指令监听（后台）
        bot_task = asyncio.create_task(self.bot.start_polling())
        try:
            await self._main_loop()
        finally:
            bot_task.cancel()

    async def stop(self):
        self._running = False
        await self.exchange.close()
        logger.info("实盘已停止")

    async def _reconcile_positions(self):
        """
        启动时同步交易所真实持仓到内存。
        恢复后立即检查是否已触发止损/止盈，提前预警。
        """
        try:
            positions = await self.exchange.client.fetch_positions(self.symbols)
        except Exception as e:
            logger.warning(f"拉取持仓失败，跳过同步: {e}")
            return

        recovered = []
        warnings = []

        for p in positions:
            sym = p.get("symbol")
            contracts = float(p.get("contracts", 0) or 0)
            if sym not in self.symbols or contracts < 1e-9:
                continue

            side = p.get("side")
            entry = float(p.get("entryPrice", 0) or 0)
            self._positions[sym] = {
                "direction": side,
                "amount": contracts,
                "avg_price": entry,
            }

            # 拉当前价 + ATR 检查是否已触发风控
            try:
                ticker = await self.exchange.fetch_ticker(sym)
                curr_price = float(ticker.get("last", entry) or entry)
                pnl = (curr_price - entry) * contracts if side == "long" else (entry - curr_price) * contracts
                pnl_pct = (pnl / (entry * contracts / self.leverage)) * 100 if entry > 0 else 0

                line = f"{sym} {side} {contracts:.4f} @ {entry:.2f} | 现价 {curr_price:.2f} | 浮盈 {pnl_pct:+.2f}%"
                recovered.append(line)
                logger.info(f"恢复持仓: {line}")

                # 提前警告：如果浮亏接近止损比例，提示用户
                if pnl_pct < -self.risk.stop_loss_pct * 100 * 0.8:
                    warnings.append(f"⚠️ {sym} 浮亏 {pnl_pct:.2f}% 接近止损线")
            except Exception as e:
                recovered.append(f"{sym} {side} {contracts:.4f} @ {entry:.2f}")
                logger.warning(f"恢复持仓但无法获取实时价: {e}")

        if recovered:
            msg = "🔄 <b>启动时恢复持仓</b>\n" + "\n".join(f"• {r}" for r in recovered)
            if warnings:
                msg += "\n\n" + "\n".join(warnings)
            await self.notifier.send(msg)

    async def _main_loop(self):
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("主循环异常: " + str(e), exc_info=True)
                await self.notifier.notify_error(str(e))
            await asyncio.sleep(self.poll_interval)

    async def _tick(self):
        balance = await self.exchange.fetch_balance()
        usdt_free = balance.get("USDT", {}).get("free", 0) or 0
        # 使用分配的资金 + 当前已用保证金作为策略权益（与其他策略隔离）
        used_margin = self._used_margin()
        equity = self.allocated_capital  # 风控基准始终用分配额度
        logger.info(
            f"[{datetime.utcnow().strftime('%H:%M:%S')}] "
            f"分配:{self.allocated_capital:.2f} | 已用:{used_margin:.2f} | "
            f"全局可用:{usdt_free:.2f}"
        )

        # 定时状态播报
        self._tick_count += 1
        if self._tick_count % self._status_interval == 0:
            prices = {}
            for symbol in self.symbols:
                try:
                    ticker = await self.exchange.fetch_ticker(symbol)
                    prices[symbol] = ticker.get("last", 0)
                except Exception:
                    pass
            await self.notifier.notify_status(equity, usdt_free, self._positions, prices)

        # 熔断检查
        if self.risk.check_drawdown(equity):
            logger.warning("熔断触发，跳过信号")
            dd = (self.risk._peak_equity - equity) / self.risk._peak_equity if self.risk._peak_equity > 0 else 0
            if self.risk._cooldown_counter == self.risk.cooldown_bars - 1:
                await self.notifier.notify_circuit_breaker(equity, dd)
            return

        for symbol in self.symbols:
            await self._process_symbol(symbol, usdt_free, equity)

    async def _process_symbol(self, symbol: str, usdt_free: float, equity: float):
        try:
            df = await self.exchange.fetch_ohlcv(symbol, self.timeframe, limit=300)
            df = add_indicators(df)
            current_price = float(df["close"].iloc[-1])
            pos = self._positions.get(symbol)

            atr = float(df["atr_14"].iloc[-1]) if "atr_14" in df.columns else 0.0

            # 止损/止盈检查
            if pos and pos["amount"] > 1e-9:
                direction = pos["direction"]
                if self.risk.check_stop_loss(pos["avg_price"], current_price, direction, atr):
                    pnl = await self._close_position(symbol, pos, current_price, "止损")
                    await self.notifier.notify_stop_loss(symbol, direction, current_price, pnl)
                    return
                if self.risk.check_take_profit(pos["avg_price"], current_price, direction, symbol):
                    pnl = await self._close_position(symbol, pos, current_price, "移动止盈")
                    await self.notifier.notify_trailing_stop(symbol, direction, current_price, pnl)
                    return

            signal = self.strategy.generate_signal(df, symbol)

            if signal == 1:
                if pos and pos["direction"] == "short":
                    pnl = await self._close_position(symbol, pos, current_price, "反手做多")
                    await self.notifier.notify_close(symbol, "short", current_price, pnl, "反手做多")
                if not self._positions.get(symbol):
                    stop = self.risk.calc_stop_price(current_price, "long", atr)
                    size = self.risk.calc_position_size(equity, current_price, stop)
                    # 仓位上限：分配资金 - 已用保证金（自己的策略额度）
                    available_capital = max(0, self.allocated_capital - self._used_margin())
                    size = min(size, available_capital * self.leverage * 0.95 / current_price)
                    if size > 0:
                        await self._open_position(symbol, "long", size, current_price)

            elif signal == -1:
                if pos and pos["direction"] == "long":
                    pnl = await self._close_position(symbol, pos, current_price, "反手做空")
                    await self.notifier.notify_close(symbol, "long", current_price, pnl, "反手做空")
                if not self._positions.get(symbol):
                    stop = self.risk.calc_stop_price(current_price, "short", atr)
                    size = self.risk.calc_position_size(equity, current_price, stop)
                    # 仓位上限：分配资金 - 已用保证金（自己的策略额度）
                    available_capital = max(0, self.allocated_capital - self._used_margin())
                    size = min(size, available_capital * self.leverage * 0.95 / current_price)
                    if size > 0:
                        await self._open_position(symbol, "short", size, current_price)

        except Exception as e:
            logger.error("处理 " + symbol + " 异常: " + str(e), exc_info=True)
            await self.notifier.notify_error(f"{symbol}: {e}")

    def _truncate_amount(self, symbol: str, amount: float) -> float:
        market = self._markets.get(symbol, {})
        min_amount = market.get("limits", {}).get("amount", {}).get("min", 0.01)
        precision = market.get("precision", {}).get("amount", 0.0001)

        if isinstance(precision, int):
            factor = 10 ** precision
            amount = int(amount * factor) / factor
        else:
            amount = int(amount / precision) * precision

        if amount < min_amount:
            logger.warning(f"下单量 {amount} 小于最小值 {min_amount}，跳过")
            return 0.0
        return amount

    async def _open_position(self, symbol: str, direction: str, amount: float, price: float):
        amount = self._truncate_amount(symbol, amount)
        if amount <= 0:
            return

        # 下单前校验杠杆
        if not await self.exchange.ensure_leverage(symbol, self.leverage, self.margin_mode):
            logger.warning(f"[开仓拒绝] {symbol} 杠杆校验失败，跳过本次开仓")
            await self.notifier.notify_error(f"{symbol} 杠杆配置异常，跳过开仓")
            return

        side = "buy" if direction == "long" else "sell"
        try:
            result = await self.exchange.create_market_order(symbol, side, amount)
            filled = result.get("average", price) or price
            filled_amount = result.get("filled", amount) or amount
            margin = filled * filled_amount / self.leverage
            self._positions[symbol] = {"direction": direction, "amount": filled_amount, "avg_price": filled}
            self.risk.reset_trailing(symbol)
            logger.success(f"[开{('多' if direction=='long' else '空')}] {symbol} {filled_amount:.4f} @ {filled:.2f} | {self.leverage}x {self.margin_mode}")

            await self.notifier.notify_open(symbol, direction, filled_amount, filled, self.leverage, margin)
        except Exception as e:
            logger.error("开仓失败 " + symbol + ": " + str(e))
            await self.notifier.notify_error(f"开仓失败 {symbol}: {e}")

    async def _close_position(self, symbol: str, pos: dict, price: float, reason: str) -> float:
        """平仓，返回 PnL"""
        side = "sell" if pos["direction"] == "long" else "buy"
        try:
            result = await self.exchange.create_market_order(symbol, side, pos["amount"])
            filled = result.get("average", price) or price
            if pos["direction"] == "long":
                pnl = (filled - pos["avg_price"]) * pos["amount"] * self.leverage
            else:
                pnl = (pos["avg_price"] - filled) * pos["amount"] * self.leverage
            self._positions.pop(symbol, None)
            self.risk.reset_trailing(symbol)
            self.bot.record_trade(symbol, pos["direction"], pnl, reason)
            logger.success(f"[平仓] {symbol} @ {filled:.2f} | PnL:{pnl:+.2f} | 原因:{reason}")
            return pnl
        except Exception as e:
            logger.error("平仓失败 " + symbol + ": " + str(e))
            await self.notifier.notify_error(f"平仓失败 {symbol}: {e}")
            return 0.0

    def _used_margin(self) -> float:
        """当前持仓占用的保证金（仅本策略）"""
        return sum(
            p["amount"] * p["avg_price"] / self.leverage
            for p in self._positions.values()
        )

    def _estimate_equity(self, usdt_free: float) -> float:
        return usdt_free + self._used_margin()
