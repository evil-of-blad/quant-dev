"""
投资组合 & 仓位管理（支持合约多空、杠杆、逐仓保证金）
"""
from dataclasses import dataclass, field
from typing import Optional
from loguru import logger
from .order import Order, OrderSide


@dataclass
class Position:
    symbol: str
    direction: str = "long"      # "long" 或 "short"
    amount: float = 0.0          # 持仓数量（基础货币，如 BTC 数量）
    avg_price: float = 0.0       # 持仓均价
    leverage: int = 1            # 杠杆倍数
    margin: float = 0.0          # 占用保证金（USDT）
    liquidation_price: float = 0.0  # 强平价格
    realized_pnl: float = 0.0   # 已实现盈亏
    unrealized_pnl: float = 0.0 # 未实现盈亏

    @property
    def notional(self) -> float:
        """名义价值 = 数量 × 均价"""
        return self.avg_price * self.amount

    def market_value(self, current_price: float) -> float:
        return current_price * self.amount

    def update_unrealized(self, current_price: float):
        if self.direction == "long":
            self.unrealized_pnl = (current_price - self.avg_price) * self.amount
        else:
            self.unrealized_pnl = (self.avg_price - current_price) * self.amount

    def calc_liquidation_price(self, maintenance_margin_rate: float = 0.004) -> float:
        """
        逐仓强平价格估算：
        多仓: entry * (1 - 1/leverage + mmr)
        空仓: entry * (1 + 1/leverage - mmr)
        """
        if self.direction == "long":
            return self.avg_price * (1 - 1 / self.leverage + maintenance_margin_rate)
        else:
            return self.avg_price * (1 + 1 / self.leverage - maintenance_margin_rate)


class Portfolio:
    """
    合约投资组合管理
    - 支持多空双向
    - 逐仓保证金模式
    - 资金费率扣除
    """

    def __init__(self, initial_capital: float):
        self.initial_capital = initial_capital
        self.cash: float = initial_capital          # 可用保证金余额
        self.positions: dict[str, Position] = {}
        self.trade_history: list[dict] = []
        self.equity_curve: list[dict] = []

    def get_position(self, symbol: str) -> Optional[Position]:
        return self.positions.get(symbol)

    def total_equity(self, prices: dict[str, float]) -> float:
        """总权益 = 可用现金 + 所有仓位的（保证金 + 未实现盈亏）"""
        pos_equity = 0.0
        for sym, pos in self.positions.items():
            price = prices.get(sym, pos.avg_price)
            pos.update_unrealized(price)
            pos_equity += pos.margin + pos.unrealized_pnl
        return self.cash + pos_equity

    def apply_order(self, order: Order, leverage: int = 1):
        sym = order.symbol
        pos = self.positions.get(sym)

        if order.side == OrderSide.BUY:
            if pos and pos.direction == "short" and pos.amount > 1e-9:
                self._close_position(order, pos)
            else:
                self._open_long(order, leverage)
        else:
            if pos and pos.direction == "long" and pos.amount > 1e-9:
                self._close_position(order, pos)
            else:
                self._open_short(order, leverage)

    def _open_long(self, order: Order, leverage: int):
        notional = order.filled_price * order.filled_amount
        margin = notional / leverage
        total_cost = margin + order.fee

        if total_cost > self.cash:
            logger.warning(f"保证金不足: 需要 {total_cost:.2f}, 可用 {self.cash:.2f}")
            return

        sym = order.symbol
        if sym not in self.positions:
            self.positions[sym] = Position(symbol=sym, direction="long", leverage=leverage)

        pos = self.positions[sym]
        total_notional = pos.avg_price * pos.amount + order.filled_price * order.filled_amount
        pos.amount += order.filled_amount
        pos.avg_price = total_notional / pos.amount if pos.amount else 0.0
        pos.margin += margin
        pos.leverage = leverage
        pos.liquidation_price = pos.calc_liquidation_price()
        self.cash -= total_cost

        self._record_trade(order, pnl=0.0, direction="long")
        logger.info(
            f"[开多] {sym} {order.filled_amount:.6f} @ {order.filled_price:.2f} "
            f"| 杠杆:{leverage}x | 保证金:{margin:.2f} | 强平价:{pos.liquidation_price:.2f}"
        )

    def _open_short(self, order: Order, leverage: int):
        notional = order.filled_price * order.filled_amount
        margin = notional / leverage
        total_cost = margin + order.fee

        if total_cost > self.cash:
            logger.warning(f"保证金不足: 需要 {total_cost:.2f}, 可用 {self.cash:.2f}")
            return

        sym = order.symbol
        if sym not in self.positions:
            self.positions[sym] = Position(symbol=sym, direction="short", leverage=leverage)

        pos = self.positions[sym]
        total_notional = pos.avg_price * pos.amount + order.filled_price * order.filled_amount
        pos.amount += order.filled_amount
        pos.avg_price = total_notional / pos.amount if pos.amount else 0.0
        pos.margin += margin
        pos.leverage = leverage
        pos.liquidation_price = pos.calc_liquidation_price()
        self.cash -= total_cost

        self._record_trade(order, pnl=0.0, direction="short")
        logger.info(
            f"[开空] {sym} {order.filled_amount:.6f} @ {order.filled_price:.2f} "
            f"| 杠杆:{leverage}x | 保证金:{margin:.2f} | 强平价:{pos.liquidation_price:.2f}"
        )

    def _close_position(self, order: Order, pos: Position):
        close_amount = min(order.filled_amount, pos.amount)

        if pos.direction == "long":
            pnl = (order.filled_price - pos.avg_price) * close_amount
        else:
            pnl = (pos.avg_price - order.filled_price) * close_amount

        pnl -= order.fee
        ratio = close_amount / pos.amount
        released_margin = pos.margin * ratio

        pos.realized_pnl += pnl
        pos.amount -= close_amount
        pos.margin -= released_margin
        self.cash += released_margin + pnl

        direction_label = "平多" if pos.direction == "long" else "平空"
        logger.info(
            f"[{direction_label}] {order.symbol} {close_amount:.6f} @ {order.filled_price:.2f} "
            f"| PnL:{pnl:+.4f} | 剩余仓位:{pos.amount:.6f}"
        )

        if pos.amount < 1e-9:
            del self.positions[order.symbol]

        self._record_trade(order, pnl=pnl, direction=pos.direction)

    def deduct_funding_rate(self, symbol: str, current_price: float, funding_rate: float):
        """扣除资金费率（每8小时，多仓付/空仓收，取决于费率正负）"""
        pos = self.positions.get(symbol)
        if not pos or pos.amount < 1e-9:
            return
        notional = current_price * pos.amount
        fee = notional * funding_rate
        # 正费率: 多仓付钱给空仓
        if pos.direction == "long":
            self.cash -= fee
            pos.realized_pnl -= fee
        else:
            self.cash += fee
            pos.realized_pnl += fee

    def check_liquidation(self, symbol: str, current_price: float) -> bool:
        """检查是否触发强平，返回 True 表示已强平"""
        pos = self.positions.get(symbol)
        if not pos or pos.amount < 1e-9:
            return False
        if pos.direction == "long" and current_price <= pos.liquidation_price:
            logger.warning(f"[强平] {symbol} 多仓强平! 价格:{current_price:.2f} 强平价:{pos.liquidation_price:.2f}")
            loss = -pos.margin  # 逐仓模式损失全部保证金
            pos.realized_pnl += loss
            self._record_trade_liquidation(symbol, current_price, loss)
            del self.positions[symbol]
            return True
        if pos.direction == "short" and current_price >= pos.liquidation_price:
            logger.warning(f"[强平] {symbol} 空仓强平! 价格:{current_price:.2f} 强平价:{pos.liquidation_price:.2f}")
            loss = -pos.margin
            pos.realized_pnl += loss
            self._record_trade_liquidation(symbol, current_price, loss)
            del self.positions[symbol]
            return True
        return False

    def _record_trade(self, order: Order, pnl: float, direction: str):
        self.trade_history.append({
            "order_id": order.order_id,
            "symbol": order.symbol,
            "side": order.side.value,
            "direction": direction,
            "amount": order.filled_amount,
            "price": order.filled_price,
            "fee": order.fee,
            "pnl": pnl,
            "timestamp": order.filled_at,
        })

    def _record_trade_liquidation(self, symbol: str, price: float, loss: float):
        self.trade_history.append({
            "order_id": "LIQUIDATION",
            "symbol": symbol,
            "side": "liquidation",
            "direction": "liquidation",
            "amount": 0,
            "price": price,
            "fee": 0,
            "pnl": loss,
            "timestamp": None,
        })

    def snapshot(self, timestamp, prices: dict[str, float]) -> float:
        equity = self.total_equity(prices)
        self.equity_curve.append({"timestamp": timestamp, "equity": equity, "cash": self.cash})
        return equity

    def summary(self) -> dict:
        closed_trades = [t for t in self.trade_history if t["pnl"] != 0 and t["side"] != "liquidation"]
        wins = [t for t in closed_trades if t["pnl"] > 0]
        losses = [t for t in closed_trades if t["pnl"] < 0]
        liquidations = len([t for t in self.trade_history if t["side"] == "liquidation"])
        return {
            "total_trades": len(closed_trades),
            "win_trades": len(wins),
            "loss_trades": len(losses),
            "liquidations": liquidations,
            "win_rate": len(wins) / len(closed_trades) if closed_trades else 0,
            "total_pnl": sum(t["pnl"] for t in self.trade_history),
            "total_fee": sum(t["fee"] for t in self.trade_history),
        }
