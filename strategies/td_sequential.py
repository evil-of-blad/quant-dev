"""
神奇九转（TD Sequential）策略
- Tom DeMark 发明的经典反转策略
- 连续9根K线收盘价低于前4根K线的收盘价 → 计数到9，做多（超卖反转）
- 连续9根K线收盘价高于前4根K线的收盘价 → 计数到9，做空（超买反转）
- 计数中途被打断则重置
"""
import pandas as pd
from .base import BaseStrategy


class TDSequentialStrategy(BaseStrategy):
    """
    参数:
        td_count (int): 触发信号的计数值，经典=9
        td_lookback (int): 对比前N根K线，经典=4
        use_rsi_filter (bool): 是否用RSI过滤（默认True）
        rsi_oversold (int): RSI超卖阈值，默认40
        rsi_overbought (int): RSI超买阈值，默认60
    """

    def __init__(self, params: dict):
        super().__init__(params)
        self.td_count = params.get("td_count", 9)
        self.td_lookback = params.get("td_lookback", 4)
        self.use_rsi_filter = params.get("use_rsi_filter", True)
        self.rsi_oversold = params.get("rsi_oversold", 40)
        self.rsi_overbought = params.get("rsi_overbought", 60)

        self._buy_count = 0   # 买入计数（连续低于前4根）
        self._sell_count = 0  # 卖出计数（连续高于前4根）

    def reset(self):
        self._buy_count = 0
        self._sell_count = 0

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> int:
        if len(df) < self.td_lookback + 2:
            return 0

        close = df["close"]
        curr_close = close.iloc[-1]
        compare_close = close.iloc[-1 - self.td_lookback]

        # --- 买入计数（下跌序列）---
        # 当前收盘 < 前4根收盘 → 买入计数+1
        if curr_close < compare_close:
            self._buy_count += 1
            self._sell_count = 0  # 重置反向计数
        # --- 卖出计数（上涨序列）---
        # 当前收盘 > 前4根收盘 → 卖出计数+1
        elif curr_close > compare_close:
            self._sell_count += 1
            self._buy_count = 0
        else:
            # 相等，重置两个计数
            self._buy_count = 0
            self._sell_count = 0

        # RSI 过滤
        rsi_ok_buy = True
        rsi_ok_sell = True
        if self.use_rsi_filter:
            rsi_col = "rsi_14"
            if rsi_col in df.columns:
                rsi = df[rsi_col].iloc[-1]
                if not pd.isna(rsi):
                    rsi_ok_buy = rsi < self.rsi_oversold
                    rsi_ok_sell = rsi > self.rsi_overbought

        # 买入信号：连续9根下跌序列完成 → 超卖反转做多
        if self._buy_count >= self.td_count and rsi_ok_buy:
            self._buy_count = 0
            return 1

        # 卖出信号：连续9根上涨序列完成 → 超买反转做空
        if self._sell_count >= self.td_count and rsi_ok_sell:
            self._sell_count = 0
            return -1

        return 0
