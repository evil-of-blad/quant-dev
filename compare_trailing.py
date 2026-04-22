"""
止盈模式对比：固定6% vs 移动止盈 vs 两者结合
"""
import os, sys
import numpy as np
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.data_feed import add_indicators
from core.backtester import Backtester
from strategies.ma_crossover import MACrossoverStrategy

SYMBOLS = ["BTC/USDT:USDT", "LINK/USDT:USDT", "BCH/USDT:USDT"]
PARAMS = {"fast_period": 15, "slow_period": 50, "trend_period": 200, "trend_filter": True}

MODES = [
    # 当前：固定6%止盈
    {"name": "固定6%（当前）", "use_trailing": False, "tp": 0.06, "trail_act": 0.03, "trail_cb": 0.02},
    # 纯移动止盈（不同参数）
    {"name": "移动3%/2%", "use_trailing": True, "tp": 9.99, "trail_act": 0.03, "trail_cb": 0.02},
    {"name": "移动5%/3%", "use_trailing": True, "tp": 9.99, "trail_act": 0.05, "trail_cb": 0.03},
    {"name": "移动8%/4%", "use_trailing": True, "tp": 9.99, "trail_act": 0.08, "trail_cb": 0.04},
    {"name": "移动10%/5%", "use_trailing": True, "tp": 9.99, "trail_act": 0.10, "trail_cb": 0.05},
    # 无止盈（对照）
    {"name": "无止盈", "use_trailing": False, "tp": 9.99, "trail_act": 0.03, "trail_cb": 0.02},
]


def make_config(mode):
    return {
        "trading": {"leverage": 3, "timeframe": "4h"},
        "backtest": {"initial_capital": 10000.0, "fee_rate": 0.0005, "slippage_pct": 0.0002, "funding_rate": 0.0001},
        "risk": {
            "max_position_pct": 0.2, "stop_loss_pct": 0.03, "take_profit_pct": mode["tp"],
            "max_drawdown_pct": 0.15, "risk_per_trade_pct": 0.01, "cooldown_bars": 100,
            "use_atr_stop": True, "atr_stop_mult": 3.0,
            "use_trailing_stop": mode["use_trailing"],
            "trailing_activation_pct": mode["trail_act"],
            "trailing_callback_pct": mode["trail_cb"],
        },
    }


def main():
    data = {}
    for sym in SYMBOLS:
        safe = sym.replace("/", "_")
        df = pd.read_parquet(f"data/{safe}_4h_2021-01-01_2025-12-31.parquet")
        data[sym] = add_indicators(df)

    print("=" * 95)
    print("止盈模式对比（MA15/50 + SMA200, 3x, BTC+LINK+BCH, 2021-2025, high/low 止损）")
    print("=" * 95)
    print(f"\n{'模式':<18}{'Return':>10}{'Sharpe':>10}{'MaxDD':>10}{'交易数':>8}{'收益/DD':>10}")
    print("-" * 68)

    for mode in MODES:
        cfg = make_config(mode)
        rets, sharpes, dds, trades = [], [], [], 0
        for sym in SYMBOLS:
            bt = Backtester(cfg)
            r = bt.run(data[sym], MACrossoverStrategy(PARAMS), sym)
            rets.append(r["total_return"])
            sharpes.append(r["sharpe_ratio"])
            dds.append(r["max_drawdown"])
            trades += r.get("total_trades", 0)

        avg_ret = np.mean(rets)
        avg_sharpe = np.mean(sharpes)
        avg_dd = np.mean(dds)
        rar = avg_ret / abs(avg_dd) if avg_dd != 0 else 0
        print(f"{mode['name']:<18}{avg_ret:>+10.1%}{avg_sharpe:>10.2f}{avg_dd:>10.1%}{trades:>8}{rar:>10.2f}")


if __name__ == "__main__":
    main()
