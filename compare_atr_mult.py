"""
high/low 止损模式下，ATR 乘数对比
"""
import os, sys, copy
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.data_feed import add_indicators
from core.backtester import Backtester
from strategies.ma_crossover import MACrossoverStrategy

SYMBOLS = ["BTC/USDT:USDT", "LINK/USDT:USDT", "BCH/USDT:USDT"]
PARAMS = {"fast_period": 15, "slow_period": 50, "trend_period": 200, "trend_filter": True}
ATR_MULTS = [2.0, 2.5, 3.0, 3.5, 4.0]


def load_data(sym):
    safe = sym.replace("/", "_")
    df = pd.read_parquet(f"data/{safe}_4h_2021-01-01_2025-12-31.parquet")
    return add_indicators(df)


def make_config(atr_mult):
    return {
        "trading": {"leverage": 3, "timeframe": "4h"},
        "backtest": {"initial_capital": 10000.0, "fee_rate": 0.0005, "slippage_pct": 0.0002, "funding_rate": 0.0001},
        "risk": {
            "max_position_pct": 0.2, "stop_loss_pct": 0.03, "take_profit_pct": 0.06,
            "max_drawdown_pct": 0.15, "risk_per_trade_pct": 0.01, "cooldown_bars": 100,
            "use_atr_stop": True, "atr_stop_mult": atr_mult, "use_trailing_stop": False,
        },
    }


def main():
    data = {sym: load_data(sym) for sym in SYMBOLS}

    print("=" * 80)
    print("ATR 乘数对比（high/low 盘中止损模式）")
    print("  MA15/50 + SMA200 | 3x | BTC+LINK+BCH 等权 | 2021-2025")
    print("=" * 80)

    print(f"\n{'ATR倍数':>8}{'收益':>10}{'Sharpe':>10}{'MaxDD':>10}{'交易数':>8}{'收益/DD':>10}")
    print("-" * 58)

    for mult in ATR_MULTS:
        cfg = make_config(mult)
        rets = []
        sharpes = []
        dds = []
        trades = 0

        for sym in SYMBOLS:
            strategy = MACrossoverStrategy(PARAMS)
            bt = Backtester(cfg)
            r = bt.run(data[sym], strategy, sym)
            rets.append(r["total_return"])
            sharpes.append(r["sharpe_ratio"])
            dds.append(r["max_drawdown"])
            trades += r.get("total_trades", 0)

        avg_ret = np.mean(rets)
        avg_sharpe = np.mean(sharpes)
        avg_dd = np.mean(dds)
        rar = avg_ret / abs(avg_dd) if avg_dd != 0 else 0

        print(f"{mult:>8.1f}{avg_ret:>+10.1%}{avg_sharpe:>10.2f}{avg_dd:>10.1%}{trades:>8}{rar:>10.2f}")


if __name__ == "__main__":
    main()
