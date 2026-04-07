"""
策略基类 — 所有自定义策略继承此类
"""
from abc import ABC, abstractmethod
import pandas as pd


class BaseStrategy(ABC):
    """
    信号约定:
        1  = 做多（买入）
       -1  = 平仓（卖出）
        0  = 不操作
    """

    def __init__(self, params: dict):
        self.params = params

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame, symbol: str) -> int:
        """
        df: 截至当前 bar 的所有历史数据（含指标）
        返回: 1 / -1 / 0
        """

    def reset(self):
        """回测重置时调用，子类可覆盖清理状态"""
        pass

    @property
    def name(self) -> str:
        return self.__class__.__name__
