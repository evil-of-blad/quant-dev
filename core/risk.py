"""
风险管理模块（合约版）
- 基于杠杆的仓位计算
- 止损/止盈
- 最大回撤熔断 + 冷却重启
- 强平价格估算
"""
from loguru import logger


class RiskManager:

    def __init__(self, config: dict):
        r = config["risk"]
        t = config["trading"]
        self.max_position_pct: float = r.get("max_position_pct", 0.2)
        self.stop_loss_pct: float = r.get("stop_loss_pct", 0.03)
        self.take_profit_pct: float = r.get("take_profit_pct", 0.06)
        self.max_drawdown_pct: float = r.get("max_drawdown_pct", 0.15)
        self.risk_per_trade_pct: float = r.get("risk_per_trade_pct", 0.01)
        self.cooldown_bars: int = r.get("cooldown_bars", 100)  # 熔断后冷却 K 线数
        self.leverage: int = t.get("leverage", 1)

        self._peak_equity: float = 0.0
        self._halted: bool = False
        self._cooldown_counter: int = 0  # 剩余冷却 K 线数

    def calc_position_size(self, equity: float, price: float, stop_price: float) -> float:
        """
        计算开仓数量（基础货币数量，如 BTC 数量）
        公式: 保证金 = equity * risk_pct / (止损比例)
              数量 = 保证金 * 杠杆 / 价格
        上限: 占用保证金不超过 equity * max_position_pct
        """
        risk_amount = equity * self.risk_per_trade_pct
        stop_pct = abs(price - stop_price) / price
        if stop_pct < 1e-6:
            return 0.0

        # 需要的保证金
        margin_needed = risk_amount / stop_pct
        # 上限保证金
        margin_max = equity * self.max_position_pct
        margin = min(margin_needed, margin_max)
        # 合约数量 = 保证金 × 杠杆 / 价格
        size = margin * self.leverage / price
        return size

    def calc_stop_price(self, entry_price: float, direction: str = "long") -> float:
        if direction == "long":
            return entry_price * (1 - self.stop_loss_pct)
        return entry_price * (1 + self.stop_loss_pct)

    def calc_take_profit_price(self, entry_price: float, direction: str = "long") -> float:
        if direction == "long":
            return entry_price * (1 + self.take_profit_pct)
        return entry_price * (1 - self.take_profit_pct)

    def check_drawdown(self, current_equity: float) -> bool:
        """
        返回 True 表示当前处于熔断冷却期，应停止交易。
        冷却结束后自动重置峰值，恢复交易。
        """
        # 冷却倒计时中
        if self._halted:
            self._cooldown_counter -= 1
            if self._cooldown_counter <= 0:
                # 冷却结束，重置峰值为当前权益，重新开始
                self._halted = False
                self._peak_equity = current_equity
                logger.info(f"[风控] 熔断冷却结束，恢复交易，重置峰值={current_equity:.2f}")
            return True

        if current_equity > self._peak_equity:
            self._peak_equity = current_equity
        if self._peak_equity == 0:
            return False

        drawdown = (self._peak_equity - current_equity) / self._peak_equity
        if drawdown >= self.max_drawdown_pct:
            logger.warning(
                f"[风控] 最大回撤触发熔断! 回撤={drawdown:.2%} | "
                f"峰值={self._peak_equity:.2f} → 当前={current_equity:.2f} | "
                f"冷却 {self.cooldown_bars} 根K线后重启"
            )
            self._halted = True
            self._cooldown_counter = self.cooldown_bars
            return True
        return False

    def check_stop_loss(self, entry_price: float, current_price: float, direction: str = "long") -> bool:
        stop = self.calc_stop_price(entry_price, direction)
        if direction == "long":
            return current_price <= stop
        return current_price >= stop

    def check_take_profit(self, entry_price: float, current_price: float, direction: str = "long") -> bool:
        tp = self.calc_take_profit_price(entry_price, direction)
        if direction == "long":
            return current_price >= tp
        return current_price <= tp

    def reset(self):
        self._peak_equity = 0.0
        self._halted = False
        self._cooldown_counter = 0
