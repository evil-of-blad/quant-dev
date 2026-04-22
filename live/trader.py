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
from core.stats import StrategyStats
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
        self.stats = StrategyStats("bollinger_or_ma")

        t_cfg = config["trading"]
        self.symbols: list[str] = t_cfg["symbols"]
        self.timeframe: str = t_cfg["timeframe"]
        self.leverage: int = t_cfg.get("leverage", 1)
        self.leverage_max: int = t_cfg.get("leverage_max", 5)  # ADX 斜率触发时的高杠杆
        self.adx_slope_thresh: float = t_cfg.get("adx_slope_thresh", 3.0)
        self.adx_min: float = t_cfg.get("adx_min", 15.0)
        self.margin_mode: str = t_cfg.get("margin_mode", "isolated")
        self.signal_interval: int = self.TIMEFRAME_SECONDS.get(self.timeframe, 14400)
        self.risk_interval: int = 1800  # 止损检查间隔 30 分钟
        self.poll_interval: int = self.risk_interval  # 主循环用快档

        # 定时播报：每天 09:00 和 17:00 北京时间（UTC 1:00 和 9:00）
        self._status_utc_hours: list = [1, 9]
        self._last_status_key: str = ""  # "YYYY-MM-DD-HH" 防重复
        self._tick_count: int = 0

        self._running = False
        self._positions: dict[str, dict] = {}

    async def start(self):
        await self.exchange.init()

        # 杠杆设为最大档（leverage_max），实际开仓时通过仓位大小控制有效杠杆
        max_lev = max(self.leverage, self.leverage_max)
        for symbol in self.symbols:
            await self.exchange.set_margin_mode(symbol, self.margin_mode, max_lev)
            await self.exchange.set_leverage(symbol, max_lev, self.margin_mode)

        self._markets = self.exchange.public.markets if hasattr(self.exchange, 'public') else {}

        # 资金分配
        await self.allocator.init(self.exchange)
        self.allocated_capital = self.allocator.get("bollinger")
        if self.allocated_capital <= 0:
            logger.error("布林带未分配资金，请检查 config.allocation.bollinger_pct")
            return

        # ★ 启动时全面检查：恢复持仓 → 检查风控 → 检查信号
        await self._startup_check()

        logger.info(
            f"实盘启动 | 策略:{self.strategy.name} | 标的:{self.symbols} "
            f"| 杠杆:{self.leverage}x | 保证金:{self.margin_mode} | 周期:{self.timeframe} "
            f"| 已恢复持仓:{len(self._positions)}"
        )

        # 启动通知
        balance = await self.exchange.fetch_balance()
        equity = balance.get("USDT", {}).get("total", 0) or 0
        await self.notifier.notify_startup(self.strategy.name, self.symbols, self.leverage, equity, self.leverage_max)

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

    async def _startup_check(self):
        """
        启动时全面检查，分三步：

        第一步：从交易所恢复持仓到内存
        第二步：对已有持仓检查是否应该止损/止盈/反手
        第三步：对无持仓的标的，用和正常开仓完全一致的逻辑检查信号
                （只看最近 2 根已收盘 K 线是否有交叉，不做 48h 回溯）
        """
        logger.info("[启动检查] 开始全面检查...")

        # ========== 第一步：恢复持仓 ==========
        try:
            positions = await self.exchange.client.fetch_positions(self.symbols)
        except Exception as e:
            logger.warning(f"[启动检查] 拉取持仓失败: {e}")
            positions = []

        for p in positions:
            sym = p.get("symbol")
            contracts = float(p.get("contracts", 0) or 0)
            if sym not in self.symbols or contracts < 1e-9:
                continue

            side = p.get("side")
            entry = float(p.get("entryPrice", 0) or 0)
            pos_lev = int(float(p.get("leverage", self.leverage) or self.leverage))
            self._positions[sym] = {
                "direction": side,
                "amount": contracts,
                "avg_price": entry,
                "leverage": pos_lev,
            }

            coin = sym.split("/")[0]
            base_amount = self._contracts_to_base(sym, contracts)
            try:
                ticker = await self.exchange.fetch_ticker(sym)
                curr_price = float(ticker.get("last", entry) or entry)
                pnl = (curr_price - entry) * base_amount if side == "long" else (entry - curr_price) * base_amount
                margin = entry * base_amount / pos_lev
                pnl_pct = (pnl / margin) * 100 if margin > 0 else 0
                logger.info(f"[启动检查] 恢复 {coin} {side} {contracts:.1f}张 @ {entry:.2f} | 现价 {curr_price:.2f} | 浮盈 {pnl_pct:+.1f}%")
            except Exception:
                logger.info(f"[启动检查] 恢复 {coin} {side} {contracts:.1f}张 @ {entry:.2f}")

        # ========== 第二步：对已有持仓检查风控 ==========
        for symbol in self.symbols:
            pos = self._positions.get(symbol)
            if not pos:
                continue

            coin = symbol.split("/")[0]
            try:
                df = await self.exchange.fetch_ohlcv(symbol, self.timeframe, limit=500)
                if len(df) > 1:
                    df = df.iloc[:-1]
                df = add_indicators(df)

                current_price = float(df["close"].iloc[-1])
                atr = float(df["atr_14"].iloc[-1]) if "atr_14" in df.columns else 0.0

                # 检查止损
                stop_price = self.risk.calc_stop_price(pos["avg_price"], pos["direction"], atr)
                if pos["direction"] == "long" and current_price <= stop_price:
                    logger.warning(f"[启动检查] {coin} 已触及止损线 ({current_price:.2f} ≤ {stop_price:.2f})，立即平仓")
                    pnl = await self._close_position(symbol, pos, current_price, "启动止损")
                    await self.notifier.send(f"🛑 启动止损 {coin} @ {current_price:.2f} | PnL {pnl:+.2f}U")
                    continue
                elif pos["direction"] == "short" and current_price >= stop_price:
                    logger.warning(f"[启动检查] {coin} 已触及止损线 ({current_price:.2f} ≥ {stop_price:.2f})，立即平仓")
                    pnl = await self._close_position(symbol, pos, current_price, "启动止损")
                    await self.notifier.send(f"🛑 启动止损 {coin} @ {current_price:.2f} | PnL {pnl:+.2f}U")
                    continue

                # 检查止盈
                if self.risk.check_take_profit(pos["avg_price"], current_price, pos["direction"], symbol):
                    logger.info(f"[启动检查] {coin} 已触及止盈，立即平仓")
                    pnl = await self._close_position(symbol, pos, current_price, "启动止盈")
                    await self.notifier.send(f"🎯 启动止盈 {coin} @ {current_price:.2f} | PnL {pnl:+.2f}U")
                    continue

                # 检查是否应该反手（当前信号和持仓方向相反）
                signal = self.strategy.generate_signal(df, symbol)
                adx_val = float(df["adx_14"].iloc[-1]) if "adx_14" in df.columns else 0

                if signal == 1 and pos["direction"] == "short" and adx_val >= self.adx_min:
                    logger.info(f"[启动检查] {coin} 有做多信号但当前持空，反手")
                    pnl = await self._close_position(symbol, pos, current_price, "启动反手做多")
                    await self.notifier.notify_close(symbol, "short", current_price, pnl, "启动反手做多")
                    # 开多
                    dyn_lev = self._calc_dynamic_leverage(df)
                    stop = self.risk.calc_stop_price(current_price, "long", atr)
                    size = self.risk.calc_position_size(self.allocated_capital, current_price, stop)
                    size = size * dyn_lev / self.leverage
                    available_capital = max(0, self.allocated_capital - self._used_margin())
                    size = min(size, available_capital * dyn_lev * 0.95 / current_price)
                    if size > 0:
                        await self._open_position(symbol, "long", size, current_price, dyn_lev)
                    continue

                elif signal == -1 and pos["direction"] == "long" and adx_val >= self.adx_min:
                    logger.info(f"[启动检查] {coin} 有做空信号但当前持多，反手")
                    pnl = await self._close_position(symbol, pos, current_price, "启动反手做空")
                    await self.notifier.notify_close(symbol, "long", current_price, pnl, "启动反手做空")
                    dyn_lev = self._calc_dynamic_leverage(df)
                    stop = self.risk.calc_stop_price(current_price, "short", atr)
                    size = self.risk.calc_position_size(self.allocated_capital, current_price, stop)
                    size = size * dyn_lev / self.leverage
                    available_capital = max(0, self.allocated_capital - self._used_margin())
                    size = min(size, available_capital * dyn_lev * 0.95 / current_price)
                    if size > 0:
                        await self._open_position(symbol, "short", size, current_price, dyn_lev)
                    continue

                logger.info(f"[启动检查] {coin} 持仓正常，继续持有")

            except Exception as e:
                logger.warning(f"[启动检查] {coin} 风控检查失败: {e}")

        # ========== 第三步：对无持仓标的，用正常信号逻辑检查 ==========
        # 只看最近 2 根已收盘 K 线是否有交叉（和 generate_signal 完全一致）
        # 不做 48h 回溯，避免重复开已止损过的仓位
        for symbol in self.symbols:
            if self._positions.get(symbol):
                continue

            coin = symbol.split("/")[0]
            try:
                df = await self.exchange.fetch_ohlcv(symbol, self.timeframe, limit=500)
                if len(df) > 1:
                    df = df.iloc[:-1]
                df = add_indicators(df)

                signal = self.strategy.generate_signal(df, symbol)
                adx_val = float(df["adx_14"].iloc[-1]) if "adx_14" in df.columns else 0
                current_price = float(df["close"].iloc[-1])
                atr = float(df["atr_14"].iloc[-1]) if "atr_14" in df.columns else 0.0

                logger.info(
                    f"[启动检查] {coin} 无持仓 | signal={signal} | ADX={adx_val:.1f} | "
                    f"price={current_price:.2f}"
                )

                if signal == 0:
                    continue
                if adx_val < self.adx_min:
                    logger.info(f"[启动检查] {coin} ADX={adx_val:.1f} < {self.adx_min}，跳过")
                    continue

                direction = "long" if signal == 1 else "short"
                dyn_lev = self._calc_dynamic_leverage(df)
                stop = self.risk.calc_stop_price(current_price, direction, atr)
                size = self.risk.calc_position_size(self.allocated_capital, current_price, stop)
                size = size * dyn_lev / self.leverage
                available_capital = max(0, self.allocated_capital - self._used_margin())
                size = min(size, available_capital * dyn_lev * 0.95 / current_price)

                if size > 0:
                    logger.info(f"[启动检查] {coin} 检测到交叉信号 → 开{direction}")
                    await self._open_position(symbol, direction, size, current_price, dyn_lev)

            except Exception as e:
                logger.warning(f"[启动检查] {coin} 信号检查失败: {e}")

        # 汇总通知
        pos_count = len(self._positions)
        if pos_count > 0:
            lines = []
            for sym, pos in self._positions.items():
                coin = sym.split("/")[0]
                d = "多" if pos["direction"] == "long" else "空"
                lines.append(f"{coin} {d} {pos['amount']:.1f}张 @ {pos['avg_price']:.2f} {pos.get('leverage', self.leverage)}x")
            await self.notifier.send(f"🔄 启动完成 | 持仓 {pos_count} 个\n" + "\n".join(f"• {l}" for l in lines))
        else:
            await self.notifier.send("🔄 启动完成 | 无持仓")

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

    def _calc_dynamic_leverage(self, df) -> int:
        """
        ADX 斜率动态杠杆：
        - ADX 3 根 K 线变化 > 阈值 且 ADX > 最低值 → 趋势刚启动 → leverage_max
        - 否则 → leverage（基础杠杆）
        """
        if "adx_14" not in df.columns or len(df) < 4:
            return self.leverage

        adx_now = float(df["adx_14"].iloc[-1])
        adx_prev = float(df["adx_14"].iloc[-4])  # 3 根前
        adx_slope = adx_now - adx_prev

        if adx_slope > self.adx_slope_thresh and adx_now > self.adx_min:
            logger.info(
                f"[动态杠杆] ADX={adx_now:.1f} 斜率={adx_slope:.1f} > {self.adx_slope_thresh} → {self.leverage_max}x"
            )
            return self.leverage_max
        return self.leverage

    def _is_signal_time(self) -> bool:
        """
        判断当前是否该检查信号。
        OKX 4h K 线在 UTC 0/4/8/12/16/20 收盘。
        fetch_ohlcv 在整点后返回的最后一根是刚开始的新 bar（未收盘），
        倒数第二根才是刚收盘的。所以 _check_signal 去掉最后一根是对的。
        检查时机也在整点后，确保倒数第二根已完整。
        """
        now = datetime.utcnow()
        return now.hour % 4 == 0 and now.minute < 35

    async def _tick(self):
        balance = await self.exchange.fetch_balance()
        usdt_free = balance.get("USDT", {}).get("free", 0) or 0
        used_margin = self._used_margin()
        equity = self.allocated_capital

        self._tick_count += 1
        is_signal_tick = self._is_signal_time()

        # 真实权益 = 可用余额 + 占用保证金（近似，因为浮盈浮亏无法精确拆分到单策略）
        equity = usdt_free + used_margin

        # 信号 tick 时打详细日志 + 每日快照
        if is_signal_tick:
            self.stats.daily_snapshot(equity)
            logger.info(
                f"[{datetime.utcnow().strftime('%H:%M:%S')}] "
                f"权益:{equity:.2f} | 分配:{self.allocated_capital:.2f} | 已用:{used_margin:.2f} | "
                f"全局可用:{usdt_free:.2f}"
            )

        # 定时播报：每天 09:00 / 17:00 北京时间（UTC 1:00 / 9:00）
        now = datetime.utcnow()
        status_key = f"{now.strftime('%Y-%m-%d')}-{now.hour}"
        if now.hour in self._status_utc_hours and status_key != self._last_status_key:
            self._last_status_key = status_key
            prices = {}
            for symbol in self.symbols:
                try:
                    ticker = await self.exchange.fetch_ticker(symbol)
                    prices[symbol] = ticker.get("last", 0)
                except Exception:
                    pass
            await self.notifier.notify_status(equity, usdt_free, self._positions, prices, self._contracts_to_base)

        # 熔断检查（用真实权益，每次 tick 都做）
        if self.risk.check_drawdown(equity):
            logger.warning(f"熔断触发，权益={equity:.2f}，跳过信号")
            dd = (self.risk._peak_equity - equity) / self.risk._peak_equity if self.risk._peak_equity > 0 else 0
            if self.risk._cooldown_counter == self.risk.cooldown_bars - 1:
                await self.notifier.notify_circuit_breaker(equity, dd)
            return

        for symbol in self.symbols:
            # 止损/止盈：每次 tick 都检查（30min）
            had_position = symbol in self._positions
            await self._check_risk(symbol)
            just_closed = had_position and symbol not in self._positions

            # 策略信号：只在 4h K 线收盘时检查
            # 如果刚止盈/止损平仓，本 tick 不再开新仓（和回测行为一致）
            if is_signal_tick and not just_closed:
                await self._check_signal(symbol, usdt_free, equity)

    async def _check_risk(self, symbol: str):
        """快档：止损止盈检查（每 30 分钟）"""
        pos = self._positions.get(symbol)
        if not pos or pos["amount"] <= 1e-9:
            return

        try:
            ticker = await self.exchange.fetch_ticker(symbol)
            current_price = float(ticker.get("last", 0) or 0)
            if current_price <= 0:
                return

            # 用最近的 ATR（从 ticker 无法拿到，用简化版：固定 ATR 比例估算）
            # 这里拉少量 K 线算 ATR，比拉 300 根快得多
            df = await self.exchange.fetch_ohlcv(symbol, self.timeframe, limit=20)
            if len(df) < 15:
                return
            import ta
            atr_series = ta.volatility.AverageTrueRange(
                df["high"], df["low"], df["close"], window=14
            ).average_true_range()
            atr = float(atr_series.iloc[-1]) if not atr_series.empty else 0.0

            direction = pos["direction"]
            if self.risk.check_stop_loss(pos["avg_price"], current_price, direction, atr):
                pnl = await self._close_position(symbol, pos, current_price, "止损")
                await self.notifier.notify_stop_loss(symbol, direction, current_price, pnl)
                return
            if self.risk.check_take_profit(pos["avg_price"], current_price, direction, symbol):
                pnl = await self._close_position(symbol, pos, current_price, "止盈")
                await self.notifier.notify_take_profit(symbol, direction, current_price, pnl)
                return

        except Exception as e:
            logger.warning(f"[风控检查] {symbol} 异常: {e}")

    async def _check_signal(self, symbol: str, usdt_free: float, equity: float):
        """慢档：策略信号检查（4h K 线收盘时）"""
        try:
            # limit=500: add_indicators 的 dropna 会丢掉 ~200 根 warmup，
            # generate_signal 需要至少 202 根，所以 500 - 200 = 300 根够用
            df = await self.exchange.fetch_ohlcv(symbol, self.timeframe, limit=500)
            # 去掉最后一根未收盘 K 线
            if len(df) > 1:
                df = df.iloc[:-1]
            df = add_indicators(df)
            current_price = float(df["close"].iloc[-1])
            pos = self._positions.get(symbol)
            atr = float(df["atr_14"].iloc[-1]) if "atr_14" in df.columns else 0.0

            # 动态杠杆
            dyn_lev = self._calc_dynamic_leverage(df)

            signal = self.strategy.generate_signal(df, symbol)

            # ADX 开仓过滤：ADX < adx_min 时不开新仓（但仍平反向仓位）
            adx_val = float(df["adx_14"].iloc[-1]) if "adx_14" in df.columns else 99
            coin = symbol.split("/")[0]
            logger.info(
                f"[信号检查] {coin} price={current_price:.2f} | signal={signal} | "
                f"ADX={adx_val:.1f} | lev={dyn_lev}x | pos={'有' if pos else '无'}"
            )

            if signal != 0 and adx_val < self.adx_min:
                logger.info(f"[信号过滤] {coin} ADX={adx_val:.1f} < {self.adx_min}，跳过开仓")
                # 仍然平反向仓位
                if signal == 1 and pos and pos["direction"] == "short":
                    pnl = await self._close_position(symbol, pos, current_price, "反手做多")
                    await self.notifier.notify_close(symbol, "short", current_price, pnl, "反手做多")
                elif signal == -1 and pos and pos["direction"] == "long":
                    pnl = await self._close_position(symbol, pos, current_price, "反手做空")
                    await self.notifier.notify_close(symbol, "long", current_price, pnl, "反手做空")
                return

            if signal == 1:
                if pos and pos["direction"] == "short":
                    pnl = await self._close_position(symbol, pos, current_price, "反手做多")
                    await self.notifier.notify_close(symbol, "short", current_price, pnl, "反手做多")
                if not self._positions.get(symbol):
                    stop = self.risk.calc_stop_price(current_price, "long", atr)
                    size = self.risk.calc_position_size(equity, current_price, stop)
                    size = size * dyn_lev / self.leverage
                    available_capital = max(0, self.allocated_capital - self._used_margin())
                    size = min(size, available_capital * dyn_lev * 0.95 / current_price)
                    if size > 0:
                        await self._open_position(symbol, "long", size, current_price, dyn_lev)

            elif signal == -1:
                if pos and pos["direction"] == "long":
                    pnl = await self._close_position(symbol, pos, current_price, "反手做空")
                    await self.notifier.notify_close(symbol, "long", current_price, pnl, "反手做空")
                if not self._positions.get(symbol):
                    stop = self.risk.calc_stop_price(current_price, "short", atr)
                    size = self.risk.calc_position_size(equity, current_price, stop)
                    size = size * dyn_lev / self.leverage
                    available_capital = max(0, self.allocated_capital - self._used_margin())
                    size = min(size, available_capital * dyn_lev * 0.95 / current_price)
                    if size > 0:
                        await self._open_position(symbol, "short", size, current_price, dyn_lev)

        except Exception as e:
            logger.error("处理 " + symbol + " 信号异常: " + str(e), exc_info=True)
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

    def _base_to_contracts(self, symbol: str, base_amount: float) -> float:
        """
        将 base coin 数量转换为 OKX 合约张数。
        OKX swap 的 amount 参数是「张数」，每张 = contractSize 个 base coin。
        例: BTC 每张=0.01 BTC, ETH 每张=0.1 ETH, DOGE 每张=1000 DOGE
        """
        market = self._markets.get(symbol, {})
        contract_size = float(market.get("contractSize", 1) or 1)
        contracts = base_amount / contract_size
        return contracts

    async def _open_position(self, symbol: str, direction: str, amount: float, price: float, lev: int = None):
        if lev is None:
            lev = self.leverage
        # amount 从 calc_position_size 来的是 base coin 数量，需要转成合约张数
        amount = self._base_to_contracts(symbol, amount)
        amount = self._truncate_amount(symbol, amount)
        if amount <= 0:
            return

        # 下单前校验杠杆
        if not await self.exchange.ensure_leverage(symbol, lev, self.margin_mode):
            logger.warning(f"[开仓拒绝] {symbol} 杠杆 {lev}x 校验失败，跳过本次开仓")
            await self.notifier.notify_error(f"{symbol} 杠杆 {lev}x 配置异常，跳过开仓")
            return

        side = "buy" if direction == "long" else "sell"
        try:
            result = await self.exchange.create_market_order(symbol, side, amount)
            filled = result.get("average", price) or price
            filled_amount = result.get("filled", amount) or amount
            base_filled = self._contracts_to_base(symbol, filled_amount)
            margin = filled * base_filled / lev
            self._positions[symbol] = {"direction": direction, "amount": filled_amount, "avg_price": filled, "leverage": lev}
            self.risk.reset_trailing(symbol)
            logger.success(
                f"[开{('多' if direction=='long' else '空')}] {symbol} "
                f"{filled_amount:.4f}张(={base_filled:.6f}) @ {filled:.2f} | "
                f"{lev}x {self.margin_mode} | 保证金:{margin:.2f}"
            )

            await self.notifier.notify_open(symbol, direction, filled_amount, filled, lev, margin)
        except Exception as e:
            logger.error("开仓失败 " + symbol + ": " + str(e))
            await self.notifier.notify_error(f"开仓失败 {symbol}: {e}")

    def _contracts_to_base(self, symbol: str, contracts: float) -> float:
        """合约张数 → base coin 数量"""
        market = self._markets.get(symbol, {})
        contract_size = float(market.get("contractSize", 1) or 1)
        return contracts * contract_size

    async def _close_position(self, symbol: str, pos: dict, price: float, reason: str) -> float:
        """平仓，返回 PnL。pos['amount'] 是合约张数。PnL = 价差 × base数量（不乘杠杆）。"""
        side = "sell" if pos["direction"] == "long" else "buy"
        try:
            result = await self.exchange.create_market_order(symbol, side, pos["amount"])
            filled = result.get("average", price) or price
            base_amount = self._contracts_to_base(symbol, pos["amount"])
            if pos["direction"] == "long":
                pnl = (filled - pos["avg_price"]) * base_amount
            else:
                pnl = (pos["avg_price"] - filled) * base_amount
            self._positions.pop(symbol, None)
            self.risk.reset_trailing(symbol)
            self.bot.record_trade(symbol, pos["direction"], pnl, reason)
            self.stats.record_trade(symbol, pos["direction"], pnl, reason=reason)
            logger.success(f"[平仓] {symbol} @ {filled:.2f} | PnL:{pnl:+.2f} | 原因:{reason}")
            return pnl
        except Exception as e:
            logger.error("平仓失败 " + symbol + ": " + str(e))
            await self.notifier.notify_error(f"平仓失败 {symbol}: {e}")
            return 0.0

    def _used_margin(self) -> float:
        """当前持仓占用的保证金（仅本策略）。amount 是合约张数，需转 base。"""
        total = 0.0
        for sym, p in self._positions.items():
            base = self._contracts_to_base(sym, p["amount"])
            lev = p.get("leverage", self.leverage)
            total += base * p["avg_price"] / lev
        return total

    def _estimate_equity(self, usdt_free: float) -> float:
        return usdt_free + self._used_margin()
