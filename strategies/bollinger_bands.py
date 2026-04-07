"""
布林带策略
- 价格触及下轨 + RSI 超卖 → 做多
- 价格触及上轨 或 穿越中轨向下 → 平仓
"""
import pandas as pd
from .base import BaseStrategy


class BollingerBandsStrategy(BaseStrategy):
    """
    参数:
        bb_period (int): 布林带周期，默认 20
        bb_std    (float): 标准差倍数，默认 2.0
        rsi_period (int): RSI 过滤周期，默认 14
        rsi_oversold (int): RSI 超卖阈值，默认 35
    """

    def __init__(self, params: dict):
        super().__init__(params)
        self.period = params.get("bb_period", 20)
        self.std_mult = params.get("bb_std", 2.0)
        self.rsi_period = params.get("rsi_period", 14)
        self.rsi_oversold = params.get("rsi_oversold", 35)
        self._in_position = None  # None / "long" / "short"

    def reset(self):
        self._in_position = None

    def _calc_bb(self, close: pd.Series):
        mid = close.rolling(self.period).mean()
        std = close.rolling(self.period).std()
        upper = mid + self.std_mult * std
        lower = mid - self.std_mult * std
        return upper, mid, lower

    def _calc_rsi(self, close: pd.Series) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(self.rsi_period).mean()
        loss = (-delta.clip(upper=0)).rolling(self.rsi_period).mean()
        rs = gain / loss.replace(0, float("inf"))
        return 100 - (100 / (1 + rs))

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> int:
        min_len = max(self.period, self.rsi_period) + 2
        if len(df) < min_len:
            return 0

        close = df["close"]
        curr_close = close.iloc[-1]

        if "bb_upper" in df.columns:
            upper = df["bb_upper"].iloc[-1]
            mid = df["bb_mid"].iloc[-1]
            lower = df["bb_lower"].iloc[-1]
        else:
            u, m, l = self._calc_bb(close)
            upper, mid, lower = u.iloc[-1], m.iloc[-1], l.iloc[-1]

        rsi_col = f"rsi_{self.rsi_period}"
        if rsi_col in df.columns:
            curr_rsi = df[rsi_col].iloc[-1]
        else:
            curr_rsi = self._calc_rsi(close).iloc[-1]

        if pd.isna(upper) or pd.isna(curr_rsi):
            return 0

        prev_close = df["close"].iloc[-2]

        # 触及下轨 + RSI 超卖 → 做多（反转信号）
        if curr_close <= lower and curr_rsi < self.rsi_oversold:
            if self._in_position != "long":
                self._in_position = "long"
                return 1

        # 触及上轨 + RSI 超买 → 做空（反转信号）
        if curr_close >= upper and curr_rsi > 100 - self.rsi_oversold:
            if self._in_position != "short":
                self._in_position = "short"
                return -1

        return 0
