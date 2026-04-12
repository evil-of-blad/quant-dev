"""快速检查当前三个标的的 MA 状态"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
from core.exchange import Exchange
from core.data_feed import get_historical_data, add_indicators

config = {
    "exchange": {"name": "okx", "api_key": "", "api_secret": "", "passphrase": "", "sandbox": True, "timeout": 30000, "market_type": "swap"},
    "trading": {"timeframe": "4h", "symbols": ["BTC/USDT:USDT"]},
}

exchange = Exchange(config)
symbols = ["BTC/USDT:USDT", "LINK/USDT:USDT", "BCH/USDT:USDT"]

for sym in symbols:
    df = get_historical_data(exchange, sym, "4h", "2024-01-01", "2026-04-12", use_cache=False)
    df = add_indicators(df)

    price = df["close"].iloc[-1]
    sma15 = df["close"].rolling(15).mean().iloc[-1]
    sma50 = df["close"].rolling(50).mean().iloc[-1]
    sma200 = df["sma_200"].iloc[-1]

    # 最近一次金叉/死叉
    fast = df["close"].rolling(15).mean()
    slow = df["close"].rolling(50).mean()
    cross = fast - slow
    cross_sign = cross.apply(lambda x: 1 if x > 0 else -1)
    changes = cross_sign.diff().abs()
    last_cross_idx = changes[changes > 0].index[-1] if (changes > 0).any() else None
    last_cross_type = "金叉" if cross.loc[last_cross_idx] > 0 else "死叉" if last_cross_idx else "无"
    bars_since = len(df) - df.index.get_loc(last_cross_idx) if last_cross_idx else -1

    trend = "上方(可做多)" if price > sma200 else "下方(可做空)"
    gap_200 = (price - sma200) / sma200 * 100
    gap_fast_slow = (sma15 - sma50) / sma50 * 100

    # 信号判定
    signal = "无信号"
    if price > sma200 and sma15 > sma50:
        signal = "持多中"
    elif price < sma200 and sma15 < sma50:
        signal = "持空中"
    elif abs(gap_200) < 3:
        signal = "价格贴近SMA200，趋势不明"

    print(f"\n{'='*50}")
    print(f"{sym}")
    print(f"  价格:     {price:.2f}")
    print(f"  SMA15:    {sma15:.2f}")
    print(f"  SMA50:    {sma50:.2f}")
    print(f"  SMA200:   {sma200:.2f}")
    print(f"  价格vs200: {gap_200:+.1f}% ({trend})")
    print(f"  快慢线差:  {gap_fast_slow:+.2f}%")
    print(f"  上次交叉:  {last_cross_type} ({bars_since} 根K线前 = ~{bars_since*4/24:.0f}天)")
    print(f"  当前状态:  {signal}")
