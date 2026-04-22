"""
Supertrend vs MA15/50 回测对比
BTC + LINK + BCH 等权，3x，2021-2025，high/low 止损
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

SYMBOLS = ["BTC/USDT:USDT", "LINK/USDT:USDT", "BCH/USDT:USDT"]
CONFIG = {
    "trading": {"leverage": 3, "timeframe": "4h"},
    "backtest": {"initial_capital": 10000.0, "fee_rate": 0.0005, "slippage_pct": 0.0002, "funding_rate": 0.0001},
    "risk": {
        "max_position_pct": 0.2, "stop_loss_pct": 0.03, "take_profit_pct": 0.06,
        "max_drawdown_pct": 0.15, "risk_per_trade_pct": 0.01, "cooldown_bars": 100,
        "use_atr_stop": True, "atr_stop_mult": 3.0, "use_trailing_stop": False,
    },
}


def calc_supertrend(df, period=14, multiplier=3.0):
    """计算 Supertrend 指标"""
    hl2 = (df["high"] + df["low"]) / 2
    atr = df["atr_14"] if "atr_14" in df.columns else pd.Series(0, index=df.index)

    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr

    supertrend = pd.Series(0.0, index=df.index)
    direction = pd.Series(1, index=df.index)  # 1=上涨(多), -1=下跌(空)

    for i in range(1, len(df)):
        # 上轨：只能下移（收紧），不能上移
        if df["close"].iloc[i - 1] > upper.iloc[i - 1]:
            upper.iloc[i] = upper.iloc[i]  # 保持
        else:
            upper.iloc[i] = min(upper.iloc[i], upper.iloc[i - 1]) if upper.iloc[i - 1] != 0 else upper.iloc[i]

        # 下轨：只能上移（收紧），不能下移
        if df["close"].iloc[i - 1] < lower.iloc[i - 1]:
            lower.iloc[i] = lower.iloc[i]
        else:
            lower.iloc[i] = max(lower.iloc[i], lower.iloc[i - 1]) if lower.iloc[i - 1] != 0 else lower.iloc[i]

        # 方向判断
        if direction.iloc[i - 1] == 1:  # 之前是多
            if df["close"].iloc[i] < lower.iloc[i]:
                direction.iloc[i] = -1  # 翻空
                supertrend.iloc[i] = upper.iloc[i]
            else:
                direction.iloc[i] = 1
                supertrend.iloc[i] = lower.iloc[i]
        else:  # 之前是空
            if df["close"].iloc[i] > upper.iloc[i]:
                direction.iloc[i] = 1  # 翻多
                supertrend.iloc[i] = lower.iloc[i]
            else:
                direction.iloc[i] = -1
                supertrend.iloc[i] = upper.iloc[i]

    return supertrend, direction


def load_data(sym):
    safe = sym.replace("/", "_")
    df = pd.read_parquet(f"data/{safe}_4h_2021-01-01_2025-12-31.parquet")
    df = add_indicators(df)
    return df


def run_backtest(df, sym, signal_func):
    portfolio = Portfolio(CONFIG["backtest"]["initial_capital"])
    risk = RiskManager(CONFIG)
    risk.reset()

    fee_rate = CONFIG["backtest"]["fee_rate"]
    slip = CONFIG["backtest"]["slippage_pct"]
    funding = CONFIG["backtest"]["funding_rate"]
    leverage = CONFIG["trading"]["leverage"]

    def slip_price(p, side):
        return p * (1 + slip) if side == "buy" else p * (1 - slip)

    def open_pos(direction, price, equity, atr):
        side = "buy" if direction == "long" else "sell"
        fp = slip_price(price, side)
        stop = risk.calc_stop_price(fp, direction, atr)
        size = risk.calc_position_size(equity, fp, stop)
        if size <= 0:
            return
        fee = fp * size * fee_rate
        order = Order(symbol=sym, side=OrderSide.BUY if direction == "long" else OrderSide.SELL,
                      order_type=OrderType.MARKET, amount=size)
        order.fill(fp, size, fee)
        portfolio.apply_order(order, leverage=leverage)
        risk.reset_trailing(sym)

    def close_pos(price, reason=""):
        pos = portfolio.get_position(sym)
        if not pos or pos.amount < 1e-9:
            return
        cs = OrderSide.SELL if pos.direction == "long" else OrderSide.BUY
        fp = slip_price(price, cs.value)
        fee = fp * pos.amount * fee_rate
        order = Order(symbol=sym, side=cs, order_type=OrderType.MARKET, amount=pos.amount, note=reason)
        order.fill(fp, pos.amount, fee)
        portfolio.apply_order(order, leverage=leverage)
        risk.reset_trailing(sym)

    for i in range(1, len(df)):
        bar = df.iloc[i]
        price = float(bar["close"])
        ts = df.index[i]
        atr = float(bar.get("atr_14", 0))
        equity = portfolio.snapshot(ts, {sym: price})

        if portfolio.check_liquidation(sym, price):
            risk.reset_trailing(sym)
            continue
        if i % 2 == 0:
            portfolio.deduct_funding_rate(sym, price, funding)
        if risk.check_drawdown(equity):
            close_pos(price, "熔断")
            continue

        pos = portfolio.get_position(sym)
        if pos and pos.amount > 1e-9:
            bar_high = float(bar.get("high", price))
            bar_low = float(bar.get("low", price))
            stop_price = risk.calc_stop_price(pos.avg_price, pos.direction, atr)
            if pos.direction == "long" and bar_low <= stop_price:
                close_pos(stop_price, "止损")
                continue
            elif pos.direction == "short" and bar_high >= stop_price:
                close_pos(stop_price, "止损")
                continue
            if risk.check_take_profit(pos.avg_price, price, pos.direction, sym):
                close_pos(price, "止盈")
                continue

        signal = signal_func(df, i)
        if signal == 1:
            if pos and pos.direction == "short" and pos.amount > 1e-9:
                close_pos(price, "反手")
            if not portfolio.get_position(sym):
                open_pos("long", price, equity, atr)
        elif signal == -1:
            if pos and pos.direction == "long" and pos.amount > 1e-9:
                close_pos(price, "反手")
            if not portfolio.get_position(sym):
                open_pos("short", price, equity, atr)

    close_pos(float(df.iloc[-1]["close"]), "回测结束")
    eq_df = pd.DataFrame(portfolio.equity_curve).set_index("timestamp")
    return eq_df


def stats(eq, initial=10000):
    final = float(eq.iloc[-1])
    total_ret = (final - initial) / initial
    rets = eq.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(365 * 6)) if rets.std() > 0 else 0
    rolling_max = eq.cummax()
    dd = (eq - rolling_max) / rolling_max
    max_dd = float(dd.min())
    # 按年
    yearly = {}
    eq.index = pd.to_datetime(eq.index)
    for year in range(2021, 2026):
        yr = eq[eq.index.year == year]
        if len(yr) > 1:
            yearly[year] = (yr.iloc[-1] - yr.iloc[0]) / yr.iloc[0]
    return {"ret": total_ret, "sharpe": sharpe, "dd": max_dd, "yearly": yearly}


def main():
    data = {sym: load_data(sym) for sym in SYMBOLS}

    # Supertrend 参数组合
    st_params = [
        (10, 2.0), (10, 3.0), (14, 2.0), (14, 3.0), (14, 4.0), (20, 3.0),
    ]

    # 预计算所有 Supertrend
    st_cache = {}
    for sym in SYMBOLS:
        df = data[sym]
        for period, mult in st_params:
            key = f"{sym}_{period}_{mult}"
            _, direction = calc_supertrend(df, period, mult)
            st_cache[key] = direction

    # 策略列表
    strategies = []

    # MA15/50 + SMA200（当前）
    def ma_signal_factory():
        strategy = MACrossoverStrategy({"fast_period": 15, "slow_period": 50, "trend_period": 200, "trend_filter": True})
        strategy.reset()
        def sig(df, i):
            return strategy.generate_signal(df.iloc[:i+1], "x")
        return sig
    strategies.append(("MA15/50+SMA200(当前)", ma_signal_factory))

    # Supertrend 各参数
    for period, mult in st_params:
        def st_signal_factory(p=period, m=mult):
            def sig(df, i, _p=p, _m=m):
                sym_key = f"placeholder_{_p}_{_m}"
                return 0  # placeholder
            return sig
        strategies.append((f"ST({period},{mult})", None))

    # Supertrend + SMA200
    for period, mult in [(14, 3.0), (14, 2.0), (10, 3.0)]:
        strategies.append((f"ST({period},{mult})+SMA200", None))

    print("=" * 100)
    print("Supertrend vs MA15/50 回测对比（3x, BTC+LINK+BCH, 2021-2025, high/low 止损）")
    print("=" * 100)
    print(f"\n{'策略':<22}{'Return':>10}{'Sharpe':>10}{'MaxDD':>10}{'收益/DD':>10}{'2021':>8}{'2022':>8}{'2023':>8}{'2024':>8}{'2025':>8}")
    print("-" * 108)

    # MA baseline
    per_sym = {}
    for sym in SYMBOLS:
        sig_func = ma_signal_factory()
        eq = run_backtest(data[sym], sym, sig_func)
        per_sym[sym] = eq["equity"]
    port = pd.concat(per_sym.values(), axis=1).ffill().bfill().mean(axis=1)
    s = stats(port)
    rar = s["ret"] / abs(s["dd"]) if s["dd"] != 0 else 0
    y = s["yearly"]
    print(f"{'MA15/50+SMA200(当前)':<22}{s['ret']:>+10.1%}{s['sharpe']:>10.2f}{s['dd']:>10.1%}{rar:>10.2f}{y.get(2021,0):>+8.1%}{y.get(2022,0):>+8.1%}{y.get(2023,0):>+8.1%}{y.get(2024,0):>+8.1%}{y.get(2025,0):>+8.1%}")

    # Supertrend 各参数
    for period, mult in st_params:
        per_sym = {}
        for sym in SYMBOLS:
            key = f"{sym}_{period}_{mult}"
            direction = st_cache[key]

            def make_st_sig(dir_series):
                def sig(df, i):
                    if i < 1:
                        return 0
                    prev_dir = int(dir_series.iloc[i-1])
                    curr_dir = int(dir_series.iloc[i])
                    if prev_dir != 1 and curr_dir == 1:
                        return 1
                    if prev_dir != -1 and curr_dir == -1:
                        return -1
                    return 0
                return sig

            eq = run_backtest(data[sym], sym, make_st_sig(direction))
            per_sym[sym] = eq["equity"]

        port = pd.concat(per_sym.values(), axis=1).ffill().bfill().mean(axis=1)
        s = stats(port)
        rar = s["ret"] / abs(s["dd"]) if s["dd"] != 0 else 0
        y = s["yearly"]
        print(f"{'ST('+str(period)+','+str(mult)+')':<22}{s['ret']:>+10.1%}{s['sharpe']:>10.2f}{s['dd']:>10.1%}{rar:>10.2f}{y.get(2021,0):>+8.1%}{y.get(2022,0):>+8.1%}{y.get(2023,0):>+8.1%}{y.get(2024,0):>+8.1%}{y.get(2025,0):>+8.1%}")

    # Supertrend + SMA200 过滤
    for period, mult in [(14, 3.0), (14, 2.0), (10, 3.0)]:
        per_sym = {}
        for sym in SYMBOLS:
            key = f"{sym}_{period}_{mult}"
            direction = st_cache[key]

            def make_st_sma200_sig(dir_series, df_ref):
                def sig(df, i):
                    if i < 1:
                        return 0
                    prev_dir = int(dir_series.iloc[i-1])
                    curr_dir = int(dir_series.iloc[i])
                    price = float(df["close"].iloc[i])
                    sma200 = float(df["sma_200"].iloc[i]) if "sma_200" in df.columns else 0
                    if sma200 == 0 or sma200 != sma200:
                        return 0
                    if prev_dir != 1 and curr_dir == 1 and price > sma200:
                        return 1
                    if prev_dir != -1 and curr_dir == -1 and price < sma200:
                        return -1
                    return 0
                return sig

            eq = run_backtest(data[sym], sym, make_st_sma200_sig(direction, data[sym]))
            per_sym[sym] = eq["equity"]

        port = pd.concat(per_sym.values(), axis=1).ffill().bfill().mean(axis=1)
        s = stats(port)
        rar = s["ret"] / abs(s["dd"]) if s["dd"] != 0 else 0
        y = s["yearly"]
        name = f"ST({period},{mult})+SMA200"
        print(f"{name:<22}{s['ret']:>+10.1%}{s['sharpe']:>10.2f}{s['dd']:>10.1%}{rar:>10.2f}{y.get(2021,0):>+8.1%}{y.get(2022,0):>+8.1%}{y.get(2023,0):>+8.1%}{y.get(2024,0):>+8.1%}{y.get(2025,0):>+8.1%}")


if __name__ == "__main__":
    main()
