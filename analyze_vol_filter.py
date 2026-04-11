"""
方案 B 验证：极端波动率减仓过滤器
- 主策略 ma_crossover 15/50 + SMA200
- BTC + LINK + BCH 等权重，3x 隔离杠杆
- 5y (2021-2025) 4h K线
- 对比 baseline vs 高 vol (>历史扩展 90 分位) 时仓位减半
"""
import os
import sys
from typing import Optional
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.data_feed import add_indicators
from core.portfolio import Portfolio
from core.risk import RiskManager
from core.order import Order, OrderSide, OrderType
from strategies.ma_crossover import MACrossoverStrategy

DATA_DIR = "data"
SYMBOLS = ["BTC/USDT:USDT", "LINK/USDT:USDT", "BCH/USDT:USDT"]
START = "2021-01-01"
END = "2025-12-31"
TIMEFRAME = "4h"

CONFIG = {
    "trading": {"leverage": 3, "timeframe": "4h"},
    "backtest": {
        "initial_capital": 10000.0,
        "fee_rate": 0.0005,
        "slippage_pct": 0.0002,
        "funding_rate": 0.0001,
    },
    "risk": {
        "max_position_pct": 0.2,
        "stop_loss_pct": 0.03,
        "take_profit_pct": 0.06,
        "max_drawdown_pct": 0.15,
        "risk_per_trade_pct": 0.01,
        "cooldown_bars": 100,
        "use_atr_stop": True,
        "atr_stop_mult": 2.5,
        "use_trailing_stop": False,
    },
}

STRATEGY_PARAMS = {
    "fast_period": 15,
    "slow_period": 50,
    "trend_period": 200,
    "trend_filter": True,
}

# 方案 B 参数
VOL_LOOKBACK = 180     # 30 days × 6 bars/day
VOL_PERCENTILES = [0.70, 0.80, 0.90]  # 三档阈值扫描
HIGH_VOL_MULT = 0.5    # 触发后仓位减半
WARMUP_BARS = 360      # 60 天 warmup 后 percentile 才有意义


def load_data(symbol: str) -> pd.DataFrame:
    safe = symbol.replace("/", "_")
    path = f"{DATA_DIR}/{safe}_{TIMEFRAME}_{START}_{END}.parquet"
    df = pd.read_parquet(path)
    df = add_indicators(df)

    # 30 天滚动实现波动率（年化）
    log_ret = np.log(df["close"] / df["close"].shift(1))
    df["realized_vol"] = log_ret.rolling(VOL_LOOKBACK).std() * np.sqrt(365 * 6)

    # 扩展窗口分位数（causal，不 lookahead）
    for pct in VOL_PERCENTILES:
        col = f"vol_pct{int(pct*100)}"
        df[col] = df["realized_vol"].expanding(min_periods=WARMUP_BARS).quantile(pct)

    df.dropna(subset=["sma_50", "sma_200", "atr_14"], inplace=True)
    return df


def run_one(df: pd.DataFrame, symbol: str, vol_threshold: Optional[float]):
    """vol_threshold=None 表示 baseline，否则用对应分位数列做高 vol 判定"""
    vol_col = f"vol_pct{int(vol_threshold*100)}" if vol_threshold is not None else None
    cfg = CONFIG
    portfolio = Portfolio(cfg["backtest"]["initial_capital"])
    risk = RiskManager(cfg)
    risk.reset()
    strategy = MACrossoverStrategy(STRATEGY_PARAMS)
    strategy.reset()

    fee_rate = cfg["backtest"]["fee_rate"]
    slip = cfg["backtest"]["slippage_pct"]
    funding = cfg["backtest"]["funding_rate"]
    leverage = cfg["trading"]["leverage"]
    funding_interval = 2  # 4h × 2 = 8h

    high_vol_open_count = 0
    total_open_count = 0

    def slip_price(p, side):
        return p * (1 + slip) if side == "buy" else p * (1 - slip)

    def open_pos(direction, price, equity, atr, mult):
        nonlocal total_open_count, high_vol_open_count
        side = "buy" if direction == "long" else "sell"
        fp = slip_price(price, side)
        stop = risk.calc_stop_price(fp, direction, atr)
        size = risk.calc_position_size(equity * mult, fp, stop)
        if size <= 0:
            return
        fee = fp * size * fee_rate
        order = Order(
            symbol=symbol,
            side=OrderSide.BUY if direction == "long" else OrderSide.SELL,
            order_type=OrderType.MARKET,
            amount=size,
        )
        order.fill(fp, size, fee)
        portfolio.apply_order(order, leverage=leverage)
        risk.reset_trailing(symbol)
        total_open_count += 1
        if mult < 1.0:
            high_vol_open_count += 1

    def close_pos(price, reason=""):
        pos = portfolio.get_position(symbol)
        if not pos or pos.amount < 1e-9:
            return
        cs = OrderSide.SELL if pos.direction == "long" else OrderSide.BUY
        fp = slip_price(price, cs.value)
        fee = fp * pos.amount * fee_rate
        order = Order(
            symbol=symbol,
            side=cs,
            order_type=OrderType.MARKET,
            amount=pos.amount,
            note=reason,
        )
        order.fill(fp, pos.amount, fee)
        portfolio.apply_order(order, leverage=leverage)
        risk.reset_trailing(symbol)

    for i in range(1, len(df)):
        bar = df.iloc[i]
        prev = df.iloc[: i + 1]
        price = float(bar["close"])
        ts = df.index[i]
        prices = {symbol: price}
        atr = float(bar.get("atr_14", 0))

        equity = portfolio.snapshot(ts, prices)

        if portfolio.check_liquidation(symbol, price):
            risk.reset_trailing(symbol)
            continue

        if i % funding_interval == 0:
            portfolio.deduct_funding_rate(symbol, price, funding)

        if risk.check_drawdown(equity):
            close_pos(price, "熔断")
            continue

        pos = portfolio.get_position(symbol)
        if pos and pos.amount > 1e-9:
            if risk.check_stop_loss(pos.avg_price, price, pos.direction, atr):
                close_pos(price, "止损")
                continue
            if risk.check_take_profit(pos.avg_price, price, pos.direction, symbol):
                close_pos(price, "止盈")
                continue

        # 仓位乘数（仅开仓时生效，已持仓不动）
        mult = 1.0
        if vol_col is not None:
            threshold_val = bar.get(vol_col, np.nan)
            if not pd.isna(threshold_val) and bar["realized_vol"] > threshold_val:
                mult = HIGH_VOL_MULT

        signal = strategy.generate_signal(prev, symbol)
        if signal == 1:
            if pos and pos.direction == "short" and pos.amount > 1e-9:
                close_pos(price, "反手做多")
            if not portfolio.get_position(symbol):
                open_pos("long", price, equity, atr, mult)
        elif signal == -1:
            if pos and pos.direction == "long" and pos.amount > 1e-9:
                close_pos(price, "反手做空")
            if not portfolio.get_position(symbol):
                open_pos("short", price, equity, atr, mult)

    last = float(df.iloc[-1]["close"])
    close_pos(last, "回测结束")

    eq_df = pd.DataFrame(portfolio.equity_curve).set_index("timestamp")
    return eq_df, total_open_count, high_vol_open_count


def stats(eq: pd.Series, initial: float) -> dict:
    final = float(eq.iloc[-1])
    total_ret = (final - initial) / initial
    rets = eq.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(365 * 6)) if rets.std() > 0 else 0.0
    rolling_max = eq.cummax()
    dd = (eq - rolling_max) / rolling_max
    max_dd = float(dd.min())
    days = max(1, (eq.index[-1] - eq.index[0]).days)
    cagr = (final / initial) ** (365 / days) - 1
    return {
        "final": final,
        "total_ret": total_ret,
        "cagr": cagr,
        "sharpe": sharpe,
        "max_dd": max_dd,
    }


def main():
    print("=" * 78)
    print("方案 B 回测：极端波动率减仓 (阈值扫描)")
    print(f"  策略: ma_crossover 15/50 + SMA200, 3x isolated")
    print(f"  标的: {', '.join(SYMBOLS)} 等权重")
    print(f"  期间: {START} → {END} (4h)")
    print(f"  过滤: 30d realized vol > expanding Nth pct → 仓位 ×{HIGH_VOL_MULT}")
    print(f"  阈值: {[int(p*100) for p in VOL_PERCENTILES]}")
    print("=" * 78)

    initial = CONFIG["backtest"]["initial_capital"]

    # 模式：None=baseline, 浮点数=对应阈值
    modes = [("baseline", None)] + [(f"pct{int(p*100)}", p) for p in VOL_PERCENTILES]

    results = {}        # mode_name -> {sym: eq_df}
    open_counts = {}    # mode_name -> {sym: (total, high_vol)}

    # 数据只加载一次
    data_cache = {sym: load_data(sym) for sym in SYMBOLS}

    for mode_name, threshold in modes:
        per_sym = {}
        per_cnt = {}
        for sym in SYMBOLS:
            df = data_cache[sym]
            eq_df, total_n, hv_n = run_one(df, sym, vol_threshold=threshold)
            per_sym[sym] = eq_df
            per_cnt[sym] = (total_n, hv_n)
            print(f"  [{mode_name:9}] {sym:18}: 终值={eq_df['equity'].iloc[-1]:8.0f}  开仓={total_n:3}  减仓={hv_n}")
        results[mode_name] = per_sym
        open_counts[mode_name] = per_cnt

    # 等权组合
    portfolio_results = {}
    for mode_name, per_sym in results.items():
        all_eqs = pd.concat(
            [eq_df["equity"].rename(sym) for sym, eq_df in per_sym.items()],
            axis=1,
        )
        all_eqs = all_eqs.ffill().bfill()
        portfolio_results[mode_name] = all_eqs.mean(axis=1)

    print()
    print("=" * 78)
    print("等权组合对比 (BTC+LINK+BCH 独立资金池均值)")
    print("=" * 78)
    print(f"{'Mode':<12}{'Final':>10}{'Return':>10}{'CAGR':>10}{'Sharpe':>10}{'MaxDD':>10}{'Trigger':>12}")

    base_s = stats(portfolio_results["baseline"], initial)
    for mode_name, _ in modes:
        s = stats(portfolio_results[mode_name], initial)
        total_open = sum(c[0] for c in open_counts[mode_name].values())
        total_hv = sum(c[1] for c in open_counts[mode_name].values())
        trigger_str = f"{total_hv}/{total_open}" if total_open else "-"
        print(f"{mode_name:<12}{s['final']:>10.0f}{s['total_ret']:>10.1%}{s['cagr']:>10.1%}{s['sharpe']:>10.2f}{s['max_dd']:>10.1%}{trigger_str:>12}")

    print()
    print("=" * 78)
    print("vs baseline 增量")
    print("=" * 78)
    print(f"{'Mode':<12}{'ΔReturn':>12}{'ΔCAGR':>12}{'ΔSharpe':>12}{'ΔMaxDD':>12}")
    for mode_name, _ in modes:
        if mode_name == "baseline":
            continue
        s = stats(portfolio_results[mode_name], initial)
        print(f"{mode_name:<12}{s['total_ret']-base_s['total_ret']:>+12.1%}{s['cagr']-base_s['cagr']:>+12.1%}{s['sharpe']-base_s['sharpe']:>+12.2f}{s['max_dd']-base_s['max_dd']:>+12.1%}")


if __name__ == "__main__":
    main()
