"""
MA 参数对比：10/30 vs 15/50（均含 SMA200 趋势过滤）
BTC + LINK + BCH 等权，3x isolated，2021-2025
"""
import os, sys
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

CONFIG = {
    "trading": {"leverage": 3, "timeframe": "4h"},
    "backtest": {"initial_capital": 10000.0, "fee_rate": 0.0005, "slippage_pct": 0.0002, "funding_rate": 0.0001},
    "risk": {
        "max_position_pct": 0.2, "stop_loss_pct": 0.03, "take_profit_pct": 0.06,
        "max_drawdown_pct": 0.15, "risk_per_trade_pct": 0.01, "cooldown_bars": 100,
        "use_atr_stop": True, "atr_stop_mult": 2.5, "use_trailing_stop": False,
    },
}

PARAM_SETS = [
    {"name": "10/30", "fast_period": 10, "slow_period": 30, "trend_period": 200, "trend_filter": True},
    {"name": "15/50", "fast_period": 15, "slow_period": 50, "trend_period": 200, "trend_filter": True},
    {"name": "10/30 无过滤", "fast_period": 10, "slow_period": 30, "trend_period": 200, "trend_filter": False},
]


def load_data(symbol: str) -> pd.DataFrame:
    safe = symbol.replace("/", "_")
    path = f"{DATA_DIR}/{safe}_4h_{START}_{END}.parquet"
    df = pd.read_parquet(path)
    df = add_indicators(df)
    # 补充 sma_15 和 sma_10（add_indicators 默认没有）
    for w in [10, 15]:
        col = f"sma_{w}"
        if col not in df.columns:
            df[col] = df["close"].rolling(w).mean()
    df.dropna(subset=["sma_50", "sma_200", "atr_14"], inplace=True)
    return df


def run_one(df: pd.DataFrame, symbol: str, params: dict) -> pd.DataFrame:
    portfolio = Portfolio(CONFIG["backtest"]["initial_capital"])
    risk = RiskManager(CONFIG)
    risk.reset()
    strategy = MACrossoverStrategy(params)
    strategy.reset()

    fee_rate = CONFIG["backtest"]["fee_rate"]
    slip = CONFIG["backtest"]["slippage_pct"]
    funding = CONFIG["backtest"]["funding_rate"]
    leverage = CONFIG["trading"]["leverage"]

    trade_count = 0

    def slip_price(p, side):
        return p * (1 + slip) if side == "buy" else p * (1 - slip)

    def open_pos(direction, price, equity, atr):
        nonlocal trade_count
        side = "buy" if direction == "long" else "sell"
        fp = slip_price(price, side)
        stop = risk.calc_stop_price(fp, direction, atr)
        size = risk.calc_position_size(equity, fp, stop)
        if size <= 0:
            return
        fee = fp * size * fee_rate
        order = Order(symbol=symbol, side=OrderSide.BUY if direction == "long" else OrderSide.SELL,
                      order_type=OrderType.MARKET, amount=size)
        order.fill(fp, size, fee)
        portfolio.apply_order(order, leverage=leverage)
        risk.reset_trailing(symbol)
        trade_count += 1

    def close_pos(price, reason=""):
        pos = portfolio.get_position(symbol)
        if not pos or pos.amount < 1e-9:
            return
        cs = OrderSide.SELL if pos.direction == "long" else OrderSide.BUY
        fp = slip_price(price, cs.value)
        fee = fp * pos.amount * fee_rate
        order = Order(symbol=symbol, side=cs, order_type=OrderType.MARKET, amount=pos.amount, note=reason)
        order.fill(fp, pos.amount, fee)
        portfolio.apply_order(order, leverage=leverage)
        risk.reset_trailing(symbol)

    for i in range(1, len(df)):
        bar = df.iloc[i]
        prev = df.iloc[:i + 1]
        price = float(bar["close"])
        ts = df.index[i]
        atr = float(bar.get("atr_14", 0))
        equity = portfolio.snapshot(ts, {symbol: price})

        if portfolio.check_liquidation(symbol, price):
            risk.reset_trailing(symbol)
            continue
        if i % 2 == 0:
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

        signal = strategy.generate_signal(prev, symbol)
        if signal == 1:
            if pos and pos.direction == "short" and pos.amount > 1e-9:
                close_pos(price, "反手")
            if not portfolio.get_position(symbol):
                open_pos("long", price, equity, atr)
        elif signal == -1:
            if pos and pos.direction == "long" and pos.amount > 1e-9:
                close_pos(price, "反手")
            if not portfolio.get_position(symbol):
                open_pos("short", price, equity, atr)

    close_pos(float(df.iloc[-1]["close"]), "回测结束")
    eq_df = pd.DataFrame(portfolio.equity_curve).set_index("timestamp")
    return eq_df, trade_count


def stats(eq: pd.Series, initial: float) -> dict:
    final = float(eq.iloc[-1])
    total_ret = (final - initial) / initial
    rets = eq.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(365 * 6)) if rets.std() > 0 else 0
    rolling_max = eq.cummax()
    dd = (eq - rolling_max) / rolling_max
    max_dd = float(dd.min())
    days = max(1, (eq.index[-1] - eq.index[0]).days)
    cagr = (final / initial) ** (365 / days) - 1

    # 按年统计
    yearly = {}
    eq_yearly = eq.copy()
    eq_yearly.index = pd.to_datetime(eq_yearly.index)
    for year in range(2021, 2026):
        yr_data = eq_yearly[eq_yearly.index.year == year]
        if len(yr_data) > 1:
            yr_ret = (yr_data.iloc[-1] - yr_data.iloc[0]) / yr_data.iloc[0]
            yearly[year] = yr_ret
        else:
            yearly[year] = 0.0

    return {"final": final, "total_ret": total_ret, "cagr": cagr, "sharpe": sharpe, "max_dd": max_dd, "yearly": yearly}


def main():
    initial = CONFIG["backtest"]["initial_capital"]
    data_cache = {sym: load_data(sym) for sym in SYMBOLS}

    print("=" * 90)
    print("MA 参数对比回测")
    print(f"  标的: {', '.join(SYMBOLS)} 等权 | 3x isolated | 2021-2025 4h")
    print("=" * 90)

    all_results = {}  # param_name -> port_eq, trades, per_year

    for params in PARAM_SETS:
        name = params["name"]
        per_sym_eq = {}
        total_trades = 0
        for sym in SYMBOLS:
            eq_df, trades = run_one(data_cache[sym], sym, params)
            per_sym_eq[sym] = eq_df
            total_trades += trades

        all_eqs = pd.concat([eq["equity"].rename(sym) for sym, eq in per_sym_eq.items()], axis=1)
        all_eqs = all_eqs.ffill().bfill()
        port_eq = all_eqs.mean(axis=1)
        s = stats(port_eq, initial)
        all_results[name] = (s, total_trades)

    # 汇总表
    print()
    print("=" * 90)
    print("等权组合对比")
    print("=" * 90)
    print(f"{'参数':<14}{'Return':>10}{'CAGR':>10}{'Sharpe':>10}{'MaxDD':>10}{'交易数':>8}{'2021':>8}{'2022':>8}{'2023':>8}{'2024':>8}{'2025':>8}")

    for params in PARAM_SETS:
        name = params["name"]
        s, trades = all_results[name]
        y = s["yearly"]
        print(
            f"{name:<14}{s['total_ret']:>10.1%}{s['cagr']:>10.1%}{s['sharpe']:>10.2f}"
            f"{s['max_dd']:>10.1%}{trades:>8}"
            f"{y.get(2021,0):>+8.1%}{y.get(2022,0):>+8.1%}{y.get(2023,0):>+8.1%}"
            f"{y.get(2024,0):>+8.1%}{y.get(2025,0):>+8.1%}"
        )

    # 关键对比
    print()
    print("=" * 90)
    print("10/30 vs 15/50 增量")
    print("=" * 90)
    s_new, t_new = all_results["10/30"]
    s_old, t_old = all_results["15/50"]
    print(f"  Return : {s_old['total_ret']:>+8.1%} → {s_new['total_ret']:>+8.1%}  (Δ {s_new['total_ret']-s_old['total_ret']:+.1%})")
    print(f"  CAGR   : {s_old['cagr']:>+8.1%} → {s_new['cagr']:>+8.1%}  (Δ {s_new['cagr']-s_old['cagr']:+.1%})")
    print(f"  Sharpe : {s_old['sharpe']:>8.2f} → {s_new['sharpe']:>8.2f}  (Δ {s_new['sharpe']-s_old['sharpe']:+.2f})")
    print(f"  MaxDD  : {s_old['max_dd']:>8.1%} → {s_new['max_dd']:>8.1%}  (Δ {s_new['max_dd']-s_old['max_dd']:+.1%})")
    print(f"  交易数 : {t_old:>8} → {t_new:>8}  (Δ {t_new-t_old:+d})")
    print()
    freq_old = 5 * 365 / t_old if t_old > 0 else 0
    freq_new = 5 * 365 / t_new if t_new > 0 else 0
    print(f"  平均交易间隔: {freq_old:.1f} 天 → {freq_new:.1f} 天")


if __name__ == "__main__":
    main()
