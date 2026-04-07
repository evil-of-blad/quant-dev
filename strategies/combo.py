"""
策略 B：多策略组合
- 同时运行双均线 + 布林带两个策略
- 两个策略信号一致 → 满仓开单
- 只有一个策略有信号 → 开单（单策略也执行，增加信号频率）
- 信号冲突（一多一空） → 跳过
"""
import pandas as pd
from .base import BaseStrategy
from .ma_crossover import MACrossoverStrategy
from .bollinger_bands import BollingerBandsStrategy


class ComboStrategy(BaseStrategy):
    """
    参数:
        fast_period, slow_period, trend_filter: 传给双均线
        bb_period, bb_std, rsi_oversold:        传给布林带
    """

    def __init__(self, params: dict):
        super().__init__(params)
        self.ma = MACrossoverStrategy(params)
        self.bb = BollingerBandsStrategy(params)

    def reset(self):
        self.ma.reset()
        self.bb.reset()

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> int:
        sig_ma = self.ma.generate_signal(df, symbol)
        sig_bb = self.bb.generate_signal(df, symbol)

        # 两个都有信号且一致 → 执行
        if sig_ma != 0 and sig_bb != 0:
            if sig_ma == sig_bb:
                return sig_ma
            else:
                return 0   # 冲突，跳过

        # 任一有信号 → 执行
        if sig_ma != 0:
            return sig_ma
        if sig_bb != 0:
            return sig_bb

        return 0
