"""
风险管理模块（合约版）
- ATR 动态止损（替代固定百分比）
- 移动止盈（Trailing Stop）
- 基于杠杆的仓位计算
- 最大回撤熔断 + 冷却重启
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
        self.cooldown_bars: int = r.get("cooldown_bars", 100)
        self.leverage: int = t.get("leverage", 1)

        # ATR 动态止损
        self.use_atr_stop: bool = r.get("use_atr_stop", True)
        self.atr_stop_mult: float = r.get("atr_stop_mult", 2.5)  # 止损 = ATR × 倍数

        # 移动止盈
        self.use_trailing_stop: bool = r.get("use_trailing_stop", True)
        self.trailing_activation_pct: float = r.get("trailing_activation_pct", 0.03)  # 盈利 3% 后激活
        self.trailing_callback_pct: float = r.get("trailing_callback_pct", 0.02)      # 从最高点回撤 2% 平仓

        self._peak_equity: float = 0.0
        self._halted: bool = False
        self._cooldown_counter: int = 0

        # 每个 symbol 的移动止盈跟踪
        self._trailing_peaks: dict[str, float] = {}  # symbol → 持仓期间最有利价格
        self._trailing_activated: dict[str, bool] = {}  # symbol → 是否已激活移动止盈

    def calc_position_size(self, equity: float, price: float, stop_price: float) -> float:
        risk_amount = equity * self.risk_per_trade_pct
        stop_pct = abs(price - stop_price) / price
        if stop_pct < 1e-6:
            return 0.0
        margin_needed = risk_amount / stop_pct
        margin_max = equity * self.max_position_pct
        margin = min(margin_needed, margin_max)
        size = margin * self.leverage / price
        return size

    # ------------------------------------------------------------------
    # 止损：ATR 动态 or 固定百分比
    # ------------------------------------------------------------------
    def calc_stop_price(self, entry_price: float, direction: str = "long", atr: float = 0.0) -> float:
        """
        计算止损价格。
        如果 use_atr_stop=True 且传入了 atr，用 ATR × 倍数；否则用固定百分比。
        """
        if self.use_atr_stop and atr > 0:
            stop_dist = atr * self.atr_stop_mult
            if direction == "long":
                return entry_price - stop_dist
            return entry_price + stop_dist
        else:
            if direction == "long":
                return entry_price * (1 - self.stop_loss_pct)
            return entry_price * (1 + self.stop_loss_pct)

    def check_stop_loss(self, entry_price: float, current_price: float,
                        direction: str = "long", atr: float = 0.0) -> bool:
        stop = self.calc_stop_price(entry_price, direction, atr)
        if direction == "long":
            return current_price <= stop
        return current_price >= stop

    # ------------------------------------------------------------------
    # 止盈：移动止盈 or 固定百分比
    # ------------------------------------------------------------------
    def calc_take_profit_price(self, entry_price: float, direction: str = "long") -> float:
        if direction == "long":
            return entry_price * (1 + self.take_profit_pct)
        return entry_price * (1 - self.take_profit_pct)

    def update_trailing_stop(self, symbol: str, entry_price: float,
                             current_price: float, direction: str) -> bool:
        """
        更新移动止盈状态，返回 True 表示触发平仓。

        逻辑:
        1. 计算当前浮动盈利百分比
        2. 盈利超过 trailing_activation_pct → 激活移动止盈
        3. 激活后，持续追踪最高（多）/最低（空）价
        4. 从最有利价格回撤超过 trailing_callback_pct → 平仓
        """
        if not self.use_trailing_stop:
            return False

        # 浮动盈利百分比
        if direction == "long":
            profit_pct = (current_price - entry_price) / entry_price
        else:
            profit_pct = (entry_price - current_price) / entry_price

        # 未激活：检查是否达到激活条件
        if not self._trailing_activated.get(symbol, False):
            if profit_pct >= self.trailing_activation_pct:
                self._trailing_activated[symbol] = True
                self._trailing_peaks[symbol] = current_price
                logger.info(
                    f"[移动止盈] {symbol} 已激活 | 盈利:{profit_pct:.2%} | "
                    f"起始跟踪价:{current_price:.2f}"
                )
            return False

        # 已激活：更新最有利价格
        peak = self._trailing_peaks.get(symbol, current_price)
        if direction == "long":
            if current_price > peak:
                self._trailing_peaks[symbol] = current_price
                peak = current_price
            # 从峰值回撤
            drawback = (peak - current_price) / peak
        else:
            if current_price < peak:
                self._trailing_peaks[symbol] = current_price
                peak = current_price
            drawback = (current_price - peak) / peak

        if drawback >= self.trailing_callback_pct:
            logger.info(
                f"[移动止盈] {symbol} 触发平仓 | 峰值:{peak:.2f} → "
                f"当前:{current_price:.2f} | 回撤:{drawback:.2%}"
            )
            return True

        return False

    def check_take_profit(self, entry_price: float, current_price: float,
                          direction: str = "long", symbol: str = "") -> bool:
        """止盈检查：移动止盈优先，否则用固定百分比"""
        if self.use_trailing_stop and symbol:
            return self.update_trailing_stop(symbol, entry_price, current_price, direction)
        # 固定止盈
        tp = self.calc_take_profit_price(entry_price, direction)
        if direction == "long":
            return current_price >= tp
        return current_price <= tp

    def reset_trailing(self, symbol: str):
        """平仓后清除该 symbol 的移动止盈状态"""
        self._trailing_peaks.pop(symbol, None)
        self._trailing_activated.pop(symbol, None)

    # ------------------------------------------------------------------
    # 熔断
    # ------------------------------------------------------------------
    def check_drawdown(self, current_equity: float) -> bool:
        if self._halted:
            self._cooldown_counter -= 1
            if self._cooldown_counter <= 0:
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

    def reset(self):
        self._peak_equity = 0.0
        self._halted = False
        self._cooldown_counter = 0
        self._trailing_peaks.clear()
        self._trailing_activated.clear()
