"""
RSI 超买超卖策略
- RSI 从超卖区（<30）回升 → 做多
- RSI 进入超买区（>70）→ 平仓
"""
import pandas as pd
from .base import BaseStrategy


class RSIStrategy(BaseStrategy):
    """
    参数:
        rsi_period   (int): RSI 计算周期，默认 14
        rsi_oversold (int): 超卖阈值，默认 30
        rsi_overbought (int): 超买阈值，默认 70
    """

    def __init__(self, params: dict):
        super().__init__(params)
        self.period = params.get("rsi_period", 14)
        self.oversold = params.get("rsi_oversold", 30)
        self.overbought = params.get("rsi_overbought", 70)

    def _calc_rsi(self, close: pd.Series) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(self.period).mean()
        loss = (-delta.clip(upper=0)).rolling(self.period).mean()
        rs = gain / loss.replace(0, float("inf"))
        return 100 - (100 / (1 + rs))

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> int:
        if len(df) < self.period + 2:
            return 0

        col = f"rsi_{self.period}"
        if col in df.columns:
            rsi = df[col]
        else:
            rsi = self._calc_rsi(df["close"])

        prev_rsi = rsi.iloc[-2]
        curr_rsi = rsi.iloc[-1]

        if pd.isna(prev_rsi) or pd.isna(curr_rsi):
            return 0

        # 从超卖回升 → 做多
        if prev_rsi < self.oversold and curr_rsi >= self.oversold:
            return 1

        # 从超买回落 → 做空
        if prev_rsi > self.overbought and curr_rsi <= self.overbought:
            return -1

        return 0
