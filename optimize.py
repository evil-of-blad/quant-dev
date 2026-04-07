"""
参数网格搜索优化
支持跨多时间段综合评分，找到在不同市场环境下都表现稳定的参数。

用法:
    python optimize.py --strategy bollinger_bands --symbol BTC/USDT:USDT
    python optimize.py --strategy bollinger_bands --symbol BTC/USDT:USDT --multi-period
"""
import sys
import os
import argparse
import yaml
import itertools
import pandas as pd
from loguru import logger
from rich.console import Console
from rich.table import Table

sys.path.insert(0, os.path.dirname(__file__))

from core.exchange import Exchange
from core.data_feed import get_historical_data, add_indicators
from core.backtester import Backtester
from strategies.ma_crossover import MACrossoverStrategy
from strategies.rsi_strategy import RSIStrategy
from strategies.bollinger_bands import BollingerBandsStrategy
from strategies.combo import ComboStrategy
from strategies.adaptive import AdaptiveStrategy
from analysis.metrics import compute_metrics

logger.remove()
logger.add(sys.stderr, level="WARNING", format="<level>{message}</level>")

PARAM_GRIDS = {
    "ma_crossover": {
        "fast_period": [5, 8, 10, 15, 20],
        "slow_period": [20, 30, 40, 50, 60],
    },
    "rsi": {
        "rsi_period":     [7, 10, 14, 21],
        "rsi_oversold":   [25, 30, 35],
        "rsi_overbought": [65, 70, 75],
    },
    "bollinger_bands": {
        "bb_period":    [10, 14, 20, 26, 30],
        "bb_std":       [1.5, 2.0, 2.5, 3.0],
        "rsi_oversold": [25, 30, 35, 40],
    },
    "combo": {
        "fast_period":  [10, 20],
        "slow_period":  [30, 50],
        "bb_period":    [14, 20],
        "bb_std":       [2.0, 2.5],
        "rsi_oversold": [30, 35],
    },
    "adaptive": {
        "adx_threshold": [20, 25, 30],
        "fast_period":   [10, 20],
        "slow_period":   [30, 50],
        "bb_period":     [14, 20],
        "bb_std":        [2.0, 2.5],
        "rsi_oversold":  [30, 35],
    },
}

STRATEGY_CLS = {
    "ma_crossover":   MACrossoverStrategy,
    "rsi":            RSIStrategy,
    "bollinger_bands": BollingerBandsStrategy,
    "combo":          ComboStrategy,
    "adaptive":       AdaptiveStrategy,
}


def run_single(config: dict, df: pd.DataFrame, strategy_name: str, params: dict, symbol: str) -> dict:
    strategy = STRATEGY_CLS[strategy_name](params)
    bt = Backtester(config)
    result = bt.run(df, strategy, symbol)
    return compute_metrics(result) if result else {}


def grid_search(
    strategy_name: str,
    symbol: str,
    config: dict,
    dfs: dict[str, pd.DataFrame],   # label → df
    multi_period: bool = False,
):
    grid = PARAM_GRIDS[strategy_name]
    keys = list(grid.keys())
    combos = list(itertools.product(*grid.values()))

    if strategy_name == "ma_crossover" and "fast_period" in keys and "slow_period" in keys:
        fi, si = keys.index("fast_period"), keys.index("slow_period")
        combos = [c for c in combos if c[fi] < c[si]]

    console = Console()
    period_labels = list(dfs.keys())
    mode = "跨周期综合评分" if multi_period else period_labels[0]
    console.print(f"\n[bold cyan]网格搜索: {strategy_name} | {symbol} | {mode} | 共 {len(combos)} 组参数[/bold cyan]\n")

    results = []
    for i, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        row = {**params}
        period_returns = []
        period_sharpes = []
        valid = True

        for label, df in dfs.items():
            r = run_single(config, df, strategy_name, params, symbol)
            if not r or r.get("total_trades", 0) < 8:
                valid = False
                break
            row[f"ret_{label}"] = r.get("total_return", 0)
            row[f"sharpe_{label}"] = r.get("sharpe_ratio", 0)
            row[f"dd_{label}"] = r.get("max_drawdown", 0)
            row[f"trades_{label}"] = r.get("total_trades", 0)
            period_returns.append(r.get("total_return", 0))
            period_sharpes.append(r.get("sharpe_ratio", 0))

        if not valid:
            continue

        # 综合评分：所有周期夏普均值，负收益周期有惩罚
        avg_sharpe = sum(period_sharpes) / len(period_sharpes)
        neg_penalty = sum(1 for r in period_returns if r < 0) * 0.3
        row["score"] = avg_sharpe - neg_penalty
        row["avg_sharpe"] = avg_sharpe
        row["min_return"] = min(period_returns)
        row["all_positive"] = all(r > 0 for r in period_returns)
        results.append(row)

        if (i + 1) % 10 == 0 or i == len(combos) - 1:
            console.print(f"  进度: {i+1}/{len(combos)}", end="\r")

    if not results:
        console.print("[red]无有效结果[/red]")
        return pd.DataFrame(), {}

    df_res = pd.DataFrame(results).sort_values("score", ascending=False).reset_index(drop=True)

    # 打印 Top 10
    console.print(f"\n[bold green]Top 10 参数组合（综合评分排序）[/bold green]\n")
    table = Table(show_header=True, header_style="bold magenta")
    for k in keys:
        table.add_column(k, justify="center", min_width=8)
    for label in period_labels:
        table.add_column(f"收益({label})", justify="right", min_width=10)
    table.add_column("均夏普", justify="right", min_width=8)
    table.add_column("综合分", justify="right", min_width=8)
    table.add_column("全正", justify="center", min_width=5)

    for _, row in df_res.head(10).iterrows():
        cols = []
        for k in keys:
            v = row[k]
            cols.append(str(int(v) if isinstance(v, float) and v.is_integer() else v))
        for label in period_labels:
            ret = row.get(f"ret_{label}", 0)
            color = "green" if ret >= 0 else "red"
            cols.append(f"[{color}]{ret:+.2%}[/{color}]")
        cols.append(f"{row['avg_sharpe']:.3f}")
        cols.append(f"{row['score']:.3f}")
        cols.append("✓" if row["all_positive"] else "✗")
        table.add_row(*cols)

    console.print(table)

    best = df_res.iloc[0]
    best_params = {k: best[k] for k in keys}
    console.print(f"\n[bold yellow]最优参数:[/bold yellow]")
    for k, v in best_params.items():
        console.print(f"  {k}: {int(v) if isinstance(v, float) and v.is_integer() else v}")
    for label in period_labels:
        console.print(f"  {label} 收益: {best.get(f'ret_{label}', 0):+.2%}  夏普: {best.get(f'sharpe_{label}', 0):.3f}")

    os.makedirs("logs", exist_ok=True)
    out = f"logs/optimize_{strategy_name}_{symbol.replace('/', '_')}.csv"
    df_res.to_csv(out, index=False)
    console.print(f"\n[dim]完整结果已保存: {out}[/dim]\n")

    return df_res, best_params


def main():
    parser = argparse.ArgumentParser(description="策略参数网格搜索")
    parser.add_argument("--strategy", default="bollinger_bands")
    parser.add_argument("--symbol", default="BTC/USDT:USDT")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--multi-period", action="store_true", help="跨多个时间段综合优化")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    ex = Exchange(config)
    tf = config["trading"]["timeframe"]

    if args.multi_period:
        periods = {
            "2024": ("2024-01-01", "2024-12-31"),
            "2025": ("2025-01-01", "2025-12-31"),
        }
    else:
        periods = {
            "2024-2025": (config["backtest"]["start_date"], config["backtest"]["end_date"]),
        }

    dfs = {}
    for label, (start, end) in periods.items():
        df = get_historical_data(ex, args.symbol, tf, start, end, use_cache=True)
        dfs[label] = add_indicators(df)

    _, best = grid_search(args.strategy, args.symbol, config, dfs, multi_period=args.multi_period)
    return best


if __name__ == "__main__":
    main()
