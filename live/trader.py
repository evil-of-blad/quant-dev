"""
实盘交易引擎（合约版）— 异步轮询，支持多空双向
"""
import asyncio
from datetime import datetime
from loguru import logger
from core.exchange import AsyncExchange
from core.data_feed import add_indicators
from core.risk import RiskManager
from core.order import Order, OrderSide, OrderType
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

        t_cfg = config["trading"]
        self.symbols: list[str] = t_cfg["symbols"]
        self.timeframe: str = t_cfg["timeframe"]
        self.leverage: int = t_cfg.get("leverage", 1)
        self.margin_mode: str = t_cfg.get("margin_mode", "isolated")
        self.poll_interval: int = self.TIMEFRAME_SECONDS.get(self.timeframe, 14400)

        self._running = False
        # symbol → {direction, amount, avg_price}
        self._positions: dict[str, dict] = {}

    async def start(self):
        await self.exchange.init()

        # 设置保证金模式 + 杠杆
        for symbol in self.symbols:
            await self.exchange.set_margin_mode(symbol, self.margin_mode)
            await self.exchange.set_leverage(symbol, self.leverage, self.margin_mode)

        # 加载市场信息，获取下单精度
        self._markets = self.exchange.public.markets if hasattr(self.exchange, 'public') else {}

        logger.info(
            f"实盘启动 | 策略:{self.strategy.name} | 标的:{self.symbols} "
            f"| 杠杆:{self.leverage}x | 保证金:{self.margin_mode} | 周期:{self.timeframe}"
        )
        self._running = True
        await self._main_loop()

    async def stop(self):
        self._running = False
        await self.exchange.close()
        logger.info("实盘已停止")

    async def _main_loop(self):
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("主循环异常: " + str(e), exc_info=True)
            await asyncio.sleep(self.poll_interval)

    async def _tick(self):
        balance = await self.exchange.fetch_balance()
        usdt_free = balance.get("USDT", {}).get("free", 0) or 0
        equity = self._estimate_equity(usdt_free)
        logger.info(f"[{datetime.utcnow().strftime('%H:%M:%S')}] 可用:{usdt_free:.2f} USDT | 估算权益:{equity:.2f}")

        if self.risk.check_drawdown(equity):
            logger.warning("熔断触发，跳过信号")
            return

        for symbol in self.symbols:
            await self._process_symbol(symbol, usdt_free, equity)

    async def _process_symbol(self, symbol: str, usdt_free: float, equity: float):
        try:
            df = await self.exchange.fetch_ohlcv(symbol, self.timeframe, limit=300)
            df = add_indicators(df)
            current_price = float(df["close"].iloc[-1])
            pos = self._positions.get(symbol)

            # 获取 ATR（用于动态止损）
            atr = float(df["atr_14"].iloc[-1]) if "atr_14" in df.columns else 0.0

            # 止损/止盈检查
            if pos and pos["amount"] > 1e-9:
                direction = pos["direction"]
                if self.risk.check_stop_loss(pos["avg_price"], current_price, direction, atr):
                    await self._close_position(symbol, pos, current_price, "止损")
                    return
                if self.risk.check_take_profit(pos["avg_price"], current_price, direction, symbol):
                    await self._close_position(symbol, pos, current_price, "移动止盈")
                    return

            signal = self.strategy.generate_signal(df, symbol)

            if signal == 1:
                if pos and pos["direction"] == "short":
                    await self._close_position(symbol, pos, current_price, "反手做多")
                if not self._positions.get(symbol):
                    stop = self.risk.calc_stop_price(current_price, "long", atr)
                    size = self.risk.calc_position_size(equity, current_price, stop)
                    size = min(size, usdt_free * self.leverage * 0.95 / current_price)
                    if size > 0:
                        await self._open_position(symbol, "long", size, current_price)

            elif signal == -1:
                if pos and pos["direction"] == "long":
                    await self._close_position(symbol, pos, current_price, "反手做空")
                if not self._positions.get(symbol):
                    stop = self.risk.calc_stop_price(current_price, "short", atr)
                    size = self.risk.calc_position_size(equity, current_price, stop)
                    size = min(size, usdt_free * self.leverage * 0.95 / current_price)
                    if size > 0:
                        await self._open_position(symbol, "short", size, current_price)

        except Exception as e:
            logger.error("处理 " + symbol + " 异常: " + str(e), exc_info=True)

    def _truncate_amount(self, symbol: str, amount: float) -> float:
        """按交易所精度截断下单量"""
        market = self._markets.get(symbol, {})
        min_amount = market.get("limits", {}).get("amount", {}).get("min", 0.01)
        precision = market.get("precision", {}).get("amount", 0.0001)

        if isinstance(precision, int):
            # 精度为小数位数
            factor = 10 ** precision
            amount = int(amount * factor) / factor
        else:
            # 精度为最小步长
            amount = int(amount / precision) * precision

        if amount < min_amount:
            logger.warning(f"下单量 {amount} 小于最小值 {min_amount}，跳过")
            return 0.0
        return amount

    async def _open_position(self, symbol: str, direction: str, amount: float, price: float):
        amount = self._truncate_amount(symbol, amount)
        if amount <= 0:
            return
        side = "buy" if direction == "long" else "sell"
        try:
            result = await self.exchange.create_market_order(symbol, side, amount)
            filled = result.get("average", price) or price
            filled_amount = result.get("filled", amount) or amount
            self._positions[symbol] = {"direction": direction, "amount": filled_amount, "avg_price": filled}
            self.risk.reset_trailing(symbol)
            logger.success(f"[开{('多' if direction=='long' else '空')}] {symbol} {filled_amount:.4f} @ {filled:.2f} | {self.leverage}x {self.margin_mode}")
        except Exception as e:
            logger.error("开仓失败 " + symbol + ": " + str(e))

    async def _close_position(self, symbol: str, pos: dict, price: float, reason: str):
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
            logger.success(f"[平仓] {symbol} @ {filled:.4f} | PnL:{pnl:+.4f} | 原因:{reason}")
        except Exception as e:
            logger.error("平仓失败 " + symbol + ": " + str(e))

    def _estimate_equity(self, usdt_free: float) -> float:
        pos_margin = sum(
            p["amount"] * p["avg_price"] / self.leverage
            for p in self._positions.values()
        )
        return usdt_free + pos_margin
