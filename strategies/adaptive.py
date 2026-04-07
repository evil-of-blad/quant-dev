"""
策略 C：自适应市场状态
- 用 ADX 判断当前是趋势行情还是震荡行情
  · ADX > adx_threshold（默认25）→ 趋势市 → 用双均线
  · ADX ≤ adx_threshold          → 震荡市 → 用布林带
"""
import pandas as pd
from .base import BaseStrategy
from .ma_crossover import MACrossoverStrategy
from .bollinger_bands import BollingerBandsStrategy


class AdaptiveStrategy(BaseStrategy):
    """
    参数:
        adx_threshold (float): ADX 趋势/震荡分界值，默认 25
        adx_period    (int):   ADX 计算周期，默认 14
        + 双均线和布林带的所有参数
    """

    def __init__(self, params: dict):
        super().__init__(params)
        self.adx_threshold: float = params.get("adx_threshold", 25.0)
        self.adx_period: int = params.get("adx_period", 14)
        self.ma = MACrossoverStrategy(params)
        self.bb = BollingerBandsStrategy(params)
        self._last_regime = None   # 记录当前市场状态（用于日志）

    def reset(self):
        self.ma.reset()
        self.bb.reset()
        self._last_regime = None

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> int:
        adx_col = f"adx_{self.adx_period}"

        if adx_col in df.columns:
            adx_val = df[adx_col].iloc[-1]
        else:
            # 临时计算（无缓存时）
            import ta
            adx_val = ta.trend.ADXIndicator(
                df["high"], df["low"], df["close"], window=self.adx_period
            ).adx().iloc[-1]

        if pd.isna(adx_val):
            return 0

        is_trending = adx_val > self.adx_threshold
        regime = "趋势" if is_trending else "震荡"

        if regime != self._last_regime:
            self._last_regime = regime

        if is_trending:
            return self.ma.generate_signal(df, symbol)
        else:
            return self.bb.generate_signal(df, symbol)
