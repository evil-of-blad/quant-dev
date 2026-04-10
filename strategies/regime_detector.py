"""
市场状态识别（Market Regime Detector）

识别 4 种市场状态:
  1. STRONG_UP   - 强势上升:    SMA200 上升 + 价格站上 + ADX>=trend_threshold
  2. STRONG_DOWN - 强势下降:    SMA200 下降 + 价格跌破 + ADX>=trend_threshold
  3. CALM_RANGE  - 平静震荡:    ATR 低位 + ADX<trend_threshold
  4. CHAOS       - 混乱/未知:   高波动 + 方向不明（建议不交易）

用法:
  detector = RegimeDetector(params)
  regime = detector.detect(df)
"""
from enum import Enum
import pandas as pd


class Regime(str, Enum):
    STRONG_UP = "strong_up"      # 🟢 强势上升
    STRONG_DOWN = "strong_down"  # 🔴 强势下降
    CALM_RANGE = "calm_range"    # 🔵 平静震荡
    CHAOS = "chaos"              # ⚪ 混乱期
    UNKNOWN = "unknown"          # 数据不足


class RegimeDetector:
    """
    参数:
        trend_period (int):     趋势均线周期，默认 200
        trend_slope_window (int): 计算斜率的窗口，默认 20
        adx_threshold (float):  ADX 趋势阈值，默认 20
        atr_lookback (int):     ATR 历史窗口（用于判断高低），默认 100
        atr_high_pct (float):   ATR 高位分位数，默认 0.75
        atr_low_pct (float):    ATR 低位分位数，默认 0.50
    """

    def __init__(self, params: dict = None):
        params = params or {}
        self.trend_period = params.get("trend_period", 200)
        self.trend_slope_window = params.get("trend_slope_window", 20)
        self.adx_threshold = params.get("adx_threshold", 20.0)
        self.atr_lookback = params.get("atr_lookback", 100)
        self.atr_high_pct = params.get("atr_high_pct", 0.75)
        self.atr_low_pct = params.get("atr_low_pct", 0.50)

    def detect(self, df: pd.DataFrame) -> Regime:
        if len(df) < max(self.trend_period, self.atr_lookback) + 5:
            return Regime.UNKNOWN

        last = df.iloc[-1]
        close = last["close"]

        # ----- 1. 趋势方向 -----
        trend_col = f"sma_{self.trend_period}"
        if trend_col not in df.columns:
            sma_long = df["close"].rolling(self.trend_period).mean()
        else:
            sma_long = df[trend_col]

        if pd.isna(sma_long.iloc[-1]):
            return Regime.UNKNOWN

        # 趋势均线斜率（最近 N 根的变化率）
        sma_now = sma_long.iloc[-1]
        sma_then = sma_long.iloc[-self.trend_slope_window]
        slope = (sma_now - sma_then) / sma_then if sma_then > 0 else 0

        above_trend = close > sma_now
        below_trend = close < sma_now
        trend_up = slope > 0.005    # 斜率 > 0.5%
        trend_down = slope < -0.005

        # ----- 2. 趋势强度 ADX -----
        adx_col = "adx_14"
        if adx_col in df.columns and not pd.isna(last[adx_col]):
            adx = last[adx_col]
        else:
            adx = 0
        is_trending = adx >= self.adx_threshold

        # ----- 3. 波动率水平（ATR 分位数）-----
        atr_col = "atr_14"
        if atr_col in df.columns:
            atr_series = df[atr_col].iloc[-self.atr_lookback:].dropna()
            if len(atr_series) > 10:
                curr_atr = atr_series.iloc[-1]
                # 归一化到价格百分比
                atr_pct = curr_atr / close
                hist_atr_pct = (atr_series / df["close"].iloc[-self.atr_lookback:]).dropna()
                high_threshold = hist_atr_pct.quantile(self.atr_high_pct)
                low_threshold = hist_atr_pct.quantile(self.atr_low_pct)
                high_volatility = atr_pct >= high_threshold
                low_volatility = atr_pct <= low_threshold
            else:
                high_volatility = False
                low_volatility = False
        else:
            high_volatility = False
            low_volatility = False

        # ----- 综合判断 -----
        # 强势上升: 趋势向上 + 价格站上 + ADX 强 + 不在极端波动
        if trend_up and above_trend and is_trending and not high_volatility:
            return Regime.STRONG_UP

        # 强势下降: 趋势向下 + 价格跌破 + ADX 强 + 不在极端波动
        if trend_down and below_trend and is_trending and not high_volatility:
            return Regime.STRONG_DOWN

        # 平静震荡: 低波动 + 弱趋势
        if low_volatility and not is_trending:
            return Regime.CALM_RANGE

        # 高波动 + 方向不明 → 混乱
        if high_volatility:
            return Regime.CHAOS

        # 默认：弱趋势中性
        return Regime.CALM_RANGE
