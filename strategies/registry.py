"""
策略注册表 — 按名称获取策略类
"""
from .ma_crossover import MACrossoverStrategy
from .rsi_strategy import RSIStrategy
from .bollinger_bands import BollingerBandsStrategy
from .combo import ComboStrategy
from .adaptive import AdaptiveStrategy
from .td_sequential import TDSequentialStrategy

STRATEGY_REGISTRY = {
    "ma_crossover": MACrossoverStrategy,
    "rsi": RSIStrategy,
    "bollinger_bands": BollingerBandsStrategy,
    "combo": ComboStrategy,
    "adaptive": AdaptiveStrategy,
    "td_sequential": TDSequentialStrategy,
}


def get_strategy(name: str, params: dict):
    cls = STRATEGY_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"未知策略: {name}. 可用: {list(STRATEGY_REGISTRY.keys())}")
    return cls(params)
