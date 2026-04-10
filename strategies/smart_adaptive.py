"""
智能自适应策略 (Smart Adaptive)

根据市场状态动态选择策略和方向:

  STRONG_UP   → 双均线，仅做多
  STRONG_DOWN → 双均线，仅做空
  CALM_RANGE  → 布林带，双向反转
  CHAOS       → 不交易（保护本金）

关键不同于旧 adaptive:
  - 使用 RegimeDetector 综合 ADX/SMA200斜率/ATR分位 多因子判断
  - 强趋势行情明确方向，避免逆势
  - 混乱期主动空仓
"""
import pandas as pd
from .base import BaseStrategy
from .ma_crossover import MACrossoverStrategy
from .bollinger_bands import BollingerBandsStrategy
from .regime_detector import RegimeDetector, Regime


class SmartAdaptiveStrategy(BaseStrategy):
    """
    参数:
        所有 RegimeDetector 参数
        + 双均线参数 (fast_period, slow_period)
        + 布林带参数 (bb_period, bb_std, rsi_oversold)
    """

    def __init__(self, params: dict):
        super().__init__(params)
        self.detector = RegimeDetector(params)

        # 不要让子策略自己做趋势过滤，因为我们用 regime 控制
        ma_params = {**params, "trend_filter": False}
        self.ma = MACrossoverStrategy(ma_params)
        self.bb = BollingerBandsStrategy(params)

        self._last_regime: Regime = None

    def reset(self):
        self.ma.reset()
        self.bb.reset()
        self._last_regime = None

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> int:
        regime = self.detector.detect(df)

        if regime == Regime.UNKNOWN:
            return 0

        # 切换 regime 时重置子策略状态（避免布林带在切换瞬间触发幻信号）
        if regime != self._last_regime:
            self._last_regime = regime
            self.bb.reset()
            self.ma.reset()

        # CHAOS：不交易
        if regime == Regime.CHAOS:
            return 0

        # 强势上升：双均线信号 + 只允许做多
        if regime == Regime.STRONG_UP:
            sig = self.ma.generate_signal(df, symbol)
            return sig if sig == 1 else 0  # 只接收做多

        # 强势下降：双均线信号 + 只允许做空
        if regime == Regime.STRONG_DOWN:
            sig = self.ma.generate_signal(df, symbol)
            return sig if sig == -1 else 0  # 只接收做空

        # 平静震荡：布林带双向反转
        if regime == Regime.CALM_RANGE:
            return self.bb.generate_signal(df, symbol)

        return 0
