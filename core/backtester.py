"""
回测引擎（合约版）
- 支持多空双向
- 杠杆、保证金、强平
- 资金费率（每8小时）
- 手续费、滑点
"""
import pandas as pd
from loguru import logger
from .portfolio import Portfolio
from .risk import RiskManager
from .order import Order, OrderSide, OrderType
from strategies.base import BaseStrategy


class Backtester:

    def __init__(self, config: dict):
        bt_cfg = config["backtest"]
        t_cfg = config["trading"]
        self.initial_capital: float = bt_cfg["initial_capital"]
        self.fee_rate: float = bt_cfg.get("fee_rate", 0.0005)
        self.slippage_pct: float = bt_cfg.get("slippage_pct", 0.0002)
        self.funding_rate: float = bt_cfg.get("funding_rate", 0.0001)
        self.leverage: int = t_cfg.get("leverage", 1)
        # 4h K线 → 每2根收一次资金费（8h/4h=2）
        self.funding_interval: int = max(1, 8 // int(t_cfg.get("timeframe", "4h").replace("h", "").replace("m", "")))

        self.portfolio = Portfolio(self.initial_capital)
        self.risk = RiskManager(config)

    # ------------------------------------------------------------------
    def run(self, df: pd.DataFrame, strategy: BaseStrategy, symbol: str) -> dict:
        logger.info(
            f"回测开始: {symbol} | {len(df)} 根K线 | 初始资金:{self.initial_capital} "
            f"| 杠杆:{self.leverage}x | 资金费率:{self.funding_rate:.4%}/8h"
        )
        self.risk.reset()
        strategy.reset()

        for i in range(1, len(df)):
            bar = df.iloc[i]
            prev_bars = df.iloc[:i + 1]
            current_price = float(bar["close"])
            timestamp = df.index[i]
            prices = {symbol: current_price}

            equity = self.portfolio.snapshot(timestamp, prices)

            # 强平检查
            if self.portfolio.check_liquidation(symbol, current_price):
                continue

            # 资金费率扣除（每 funding_interval 根K线收一次）
            if i % self.funding_interval == 0:
                self.portfolio.deduct_funding_rate(symbol, current_price, self.funding_rate)

            # 熔断检查
            if self.risk.check_drawdown(equity):
                self._close_position(symbol, current_price, timestamp, reason="熔断")
                continue

            pos = self.portfolio.get_position(symbol)

            # 持仓中：止损/止盈
            if pos and pos.amount > 1e-9:
                if self.risk.check_stop_loss(pos.avg_price, current_price, pos.direction):
                    self._close_position(symbol, current_price, timestamp, reason="止损")
                    continue
                if self.risk.check_take_profit(pos.avg_price, current_price, pos.direction):
                    self._close_position(symbol, current_price, timestamp, reason="止盈")
                    continue

            # 策略信号: 1=做多, -1=做空, 0=不动
            signal = strategy.generate_signal(prev_bars, symbol)

            if signal == 1:
                if pos and pos.direction == "short" and pos.amount > 1e-9:
                    self._close_position(symbol, current_price, timestamp, reason="反手做多")
                if not self.portfolio.get_position(symbol):
                    self._open_position(symbol, "long", current_price, equity, timestamp)

            elif signal == -1:
                if pos and pos.direction == "long" and pos.amount > 1e-9:
                    self._close_position(symbol, current_price, timestamp, reason="反手做空")
                if not self.portfolio.get_position(symbol):
                    self._open_position(symbol, "short", current_price, equity, timestamp)

        # 回测结束平仓
        last_price = float(df.iloc[-1]["close"])
        self._close_position(symbol, last_price, df.index[-1], reason="回测结束")

        result = self._build_result(symbol)
        logger.info(
            f"回测完成: 最终权益 {result['final_equity']:.2f}, "
            f"收益率 {result['total_return']:.2%}, "
            f"强平次数 {result.get('liquidations', 0)}"
        )
        return result

    # ------------------------------------------------------------------
    def _apply_slippage(self, price: float, side: str) -> float:
        if side == "buy":
            return price * (1 + self.slippage_pct)
        return price * (1 - self.slippage_pct)

    def _open_position(self, symbol: str, direction: str, price: float, equity: float, timestamp):
        side = "buy" if direction == "long" else "sell"
        filled_price = self._apply_slippage(price, side)
        stop_price = self.risk.calc_stop_price(filled_price, direction)
        size = self.risk.calc_position_size(equity, filled_price, stop_price)
        if size <= 0:
            return
        fee = filled_price * size * self.fee_rate
        order = Order(symbol=symbol, side=OrderSide.BUY if direction == "long" else OrderSide.SELL,
                      order_type=OrderType.MARKET, amount=size)
        order.fill(filled_price, size, fee)
        self.portfolio.apply_order(order, leverage=self.leverage)

    def _close_position(self, symbol: str, price: float, timestamp, reason: str = ""):
        pos = self.portfolio.get_position(symbol)
        if not pos or pos.amount < 1e-9:
            return
        # 平多用卖单，平空用买单
        close_side = OrderSide.SELL if pos.direction == "long" else OrderSide.BUY
        filled_price = self._apply_slippage(price, close_side.value)
        fee = filled_price * pos.amount * self.fee_rate
        order = Order(symbol=symbol, side=close_side, order_type=OrderType.MARKET,
                      amount=pos.amount, note=reason)
        order.fill(filled_price, pos.amount, fee)
        self.portfolio.apply_order(order, leverage=self.leverage)

    # ------------------------------------------------------------------
    def _build_result(self, symbol: str) -> dict:
        equity_df = pd.DataFrame(self.portfolio.equity_curve)
        if equity_df.empty:
            return {}
        equity_df.set_index("timestamp", inplace=True)
        summary = self.portfolio.summary()

        final_equity = equity_df["equity"].iloc[-1]
        total_return = (final_equity - self.initial_capital) / self.initial_capital
        returns = equity_df["equity"].pct_change().dropna()
        sharpe = (returns.mean() / returns.std() * (365 * 6) ** 0.5) if returns.std() > 0 else 0
        rolling_max = equity_df["equity"].cummax()
        drawdown = (equity_df["equity"] - rolling_max) / rolling_max
        max_drawdown = drawdown.min()

        return {
            "symbol": symbol,
            "initial_capital": self.initial_capital,
            "final_equity": final_equity,
            "total_return": total_return,
            "sharpe_ratio": sharpe,
            "max_drawdown": max_drawdown,
            "calmar_ratio": -total_return / max_drawdown if max_drawdown != 0 else 0,
            "equity_curve": equity_df,
            "trades": self.portfolio.trade_history,
            "drawdown_series": drawdown,
            **summary,
        }
