"""止盈百分比扫描"""
import os, sys
import numpy as np
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.data_feed import add_indicators
from core.backtester import Backtester
from strategies.ma_crossover import MACrossoverStrategy

SYMBOLS = ["BTC/USDT:USDT", "LINK/USDT:USDT", "BCH/USDT:USDT"]
PARAMS = {"fast_period": 15, "slow_period": 50, "trend_period": 200, "trend_filter": True}
TP_LIST = [0.04, 0.06, 0.08, 0.10, 0.15, 0.20, 999]  # 999 = 无止盈

def make_config(tp):
    return {
        "trading": {"leverage": 3, "timeframe": "4h"},
        "backtest": {"initial_capital": 10000.0, "fee_rate": 0.0005, "slippage_pct": 0.0002, "funding_rate": 0.0001},
        "risk": {
            "max_position_pct": 0.2, "stop_loss_pct": 0.03, "take_profit_pct": tp,
            "max_drawdown_pct": 0.15, "risk_per_trade_pct": 0.01, "cooldown_bars": 100,
            "use_atr_stop": True, "atr_stop_mult": 3.0, "use_trailing_stop": False,
        },
    }

data = {}
for sym in SYMBOLS:
    safe = sym.replace("/", "_")
    df = pd.read_parquet(f"data/{safe}_4h_2021-01-01_2025-12-31.parquet")
    data[sym] = add_indicators(df)

print(f"{'止盈%':>8}{'Return':>10}{'Sharpe':>10}{'MaxDD':>10}{'交易数':>8}{'收益/DD':>10}")
print("-" * 58)

for tp in TP_LIST:
    cfg = make_config(tp)
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
    label = "无" if tp > 1 else f"{tp:.0%}"
    print(f"{label:>8}{avg_ret:>+10.1%}{avg_sharpe:>10.2f}{avg_dd:>10.1%}{trades:>8}{rar:>10.2f}")
