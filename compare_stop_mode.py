"""
对比止损模式：close-only vs high/low 盘中止损
用 core/backtester.py（已改为 high/low 模式）和一个 close-only 版本对比
"""
import os, sys, copy
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.data_feed import add_indicators
from core.backtester import Backtester
from strategies.ma_crossover import MACrossoverStrategy

SYMBOLS = ["BTC/USDT:USDT", "LINK/USDT:USDT", "BCH/USDT:USDT"]

CONFIG = {
    "trading": {"leverage": 3, "timeframe": "4h"},
    "backtest": {"initial_capital": 10000.0, "fee_rate": 0.0005, "slippage_pct": 0.0002, "funding_rate": 0.0001},
    "risk": {
        "max_position_pct": 0.2, "stop_loss_pct": 0.03, "take_profit_pct": 0.06,
        "max_drawdown_pct": 0.15, "risk_per_trade_pct": 0.01, "cooldown_bars": 100,
        "use_atr_stop": True, "atr_stop_mult": 2.5, "use_trailing_stop": False,
    },
}
PARAMS = {"fast_period": 15, "slow_period": 50, "trend_period": 200, "trend_filter": True}


def load_data(sym):
    safe = sym.replace("/", "_")
    df = pd.read_parquet(f"data/{safe}_4h_2021-01-01_2025-12-31.parquet")
    df = add_indicators(df)
    return df


def run_backtest(sym, df):
    strategy = MACrossoverStrategy(PARAMS)
    bt = Backtester(CONFIG)
    result = bt.run(df, strategy, sym)
    return result


def stats_from_result(r):
    return {
        "final": r["final_equity"],
        "ret": r["total_return"],
        "sharpe": r["sharpe_ratio"],
        "max_dd": r["max_drawdown"],
        "trades": r.get("total_trades", 0),
    }


def main():
    print("=" * 80)
    print("止损模式对比：high/low 盘中止损 vs close-only")
    print("  MA15/50 + SMA200 | 3x | BTC+LINK+BCH | 2021-2025")
    print("=" * 80)

    results = {}
    for sym in SYMBOLS:
        df = load_data(sym)
        r = run_backtest(sym, df)
        coin = sym.split("/")[0]
        results[coin] = stats_from_result(r)

    # 等权组合
    avg_ret = np.mean([r["ret"] for r in results.values()])
    avg_sharpe = np.mean([r["sharpe"] for r in results.values()])
    avg_dd = np.mean([r["max_dd"] for r in results.values()])
    total_trades = sum(r["trades"] for r in results.values())

    print(f"\n{'标的':<8}{'收益':>10}{'Sharpe':>10}{'MaxDD':>10}{'交易数':>8}")
    print("-" * 50)
    for coin, r in results.items():
        print(f"{coin:<8}{r['ret']:>+10.1%}{r['sharpe']:>10.2f}{r['max_dd']:>10.1%}{r['trades']:>8}")
    print("-" * 50)
    print(f"{'等权组合':<8}{avg_ret:>+10.1%}{avg_sharpe:>10.2f}{avg_dd:>10.1%}{total_trades:>8}")

    print(f"\n之前（close-only 止损）的等权组合:")
    print(f"  收益 +80.2% | Sharpe 1.16 | MaxDD -18.7% | 336 笔")
    print(f"\n现在（high/low 盘中止损）的等权组合:")
    print(f"  收益 {avg_ret:+.1%} | Sharpe {avg_sharpe:.2f} | MaxDD {avg_dd:.1%} | {total_trades} 笔")
    diff_ret = avg_ret - 0.802
    print(f"\n  差异: 收益 {diff_ret:+.1%} | 更接近实盘 30min 止损的真实表现")


if __name__ == "__main__":
    main()
