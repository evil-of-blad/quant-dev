"""
双均线交叉策略（含 200 均线趋势过滤）
- 金叉 + 价格在 SMA200 上方 → 做多
- 死叉 + 价格在 SMA200 下方 → 做空
- 趋势过滤可通过 trend_filter=False 关闭
"""
import pandas as pd
from .base import BaseStrategy


class MACrossoverStrategy(BaseStrategy):
    """
    参数:
        fast_period  (int):  快线周期，默认 20
        slow_period  (int):  慢线周期，默认 30
        trend_period (int):  趋势过滤均线周期，默认 200
        trend_filter (bool): 是否开启趋势过滤，默认 True
    """

    def __init__(self, params: dict):
        super().__init__(params)
        self.fast = params.get("fast_period", 20)
        self.slow = params.get("slow_period", 30)
        self.trend_period = params.get("trend_period", 200)
        self.trend_filter = params.get("trend_filter", True)

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> int:
        min_len = max(self.slow, self.trend_period if self.trend_filter else 0) + 2
        if len(df) < min_len:
            return 0

        fast_col = f"sma_{self.fast}"
        slow_col = f"sma_{self.slow}"
        trend_col = f"sma_{self.trend_period}"

        fast_ma = df[fast_col] if fast_col in df.columns else df["close"].rolling(self.fast).mean()
        slow_ma = df[slow_col] if slow_col in df.columns else df["close"].rolling(self.slow).mean()

        prev_fast = fast_ma.iloc[-2]
        prev_slow = slow_ma.iloc[-2]
        curr_fast = fast_ma.iloc[-1]
        curr_slow = slow_ma.iloc[-1]
        curr_price = df["close"].iloc[-1]

        if pd.isna(prev_fast) or pd.isna(prev_slow):
            return 0

        # 趋势方向判断（SMA200）
        if self.trend_filter:
            if trend_col in df.columns:
                trend_ma = df[trend_col].iloc[-1]
            else:
                trend_ma = df["close"].rolling(self.trend_period).mean().iloc[-1]
            if pd.isna(trend_ma):
                return 0
            above_trend = curr_price > trend_ma   # 价格在 200 均线上方 → 牛市
            below_trend = curr_price < trend_ma   # 价格在 200 均线下方 → 熊市
        else:
            above_trend = True
            below_trend = True

        # 金叉：只有在牛市区才做多
        if prev_fast <= prev_slow and curr_fast > curr_slow:
            if above_trend:
                return 1

        # 死叉：只有在熊市区才做空
        if prev_fast >= prev_slow and curr_fast < curr_slow:
            if below_trend:
                return -1

        return 0
