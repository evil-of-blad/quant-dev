"""
Kelly 公式仓位管理回测
对比不同 risk_per_trade_pct：1%（当前）vs Kelly 各比例
ATR 3.0 + high/low 止损 + ADX≥15 + 固定6%止盈
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
PARAMS = {"fast_period": 15, "slow_period": 50, "trend_period": 200, "trend_filter": True}
ADX_MIN = 15.0

# Kelly = 10.9%，测试不同比例
RISK_LEVELS = [
    ("1.0%(当前)", 0.01, 0.20),      # 当前：1% 风险，20% 最大仓位
    ("1.5%", 0.015, 0.25),
    ("2.0%(1/5 Kelly)", 0.02, 0.30),
    ("2.7%(1/4 Kelly)", 0.027, 0.35),
    ("3.5%", 0.035, 0.40),
    ("5.5%(半 Kelly)", 0.055, 0.50),
    ("8.0%(3/4 Kelly)", 0.08, 0.60),
    ("10.9%(全 Kelly)", 0.109, 0.70),
]


def make_config(risk_pct, max_pos_pct):
    return {
        "trading": {"leverage": 3, "timeframe": "4h"},
        "backtest": {"initial_capital": 10000.0, "fee_rate": 0.0005, "slippage_pct": 0.0002, "funding_rate": 0.0001},
        "risk": {
            "max_position_pct": max_pos_pct,
            "stop_loss_pct": 0.03,
            "take_profit_pct": 0.06,
            "max_drawdown_pct": 0.15,
            "risk_per_trade_pct": risk_pct,
            "cooldown_bars": 100,
            "use_atr_stop": True, "atr_stop_mult": 3.0, "use_trailing_stop": False,
        },
    }


def load_data(sym):
    safe = sym.replace("/", "_")
    df = pd.read_parquet(f"data/{safe}_4h_2021-01-01_2025-12-31.parquet")
    return add_indicators(df)


def run_one(df, sym, config):
    portfolio = Portfolio(config["backtest"]["initial_capital"])
    risk = RiskManager(config)
    risk.reset()
    strategy = MACrossoverStrategy(PARAMS)
    strategy.reset()

    fee_rate = config["backtest"]["fee_rate"]
    slip = config["backtest"]["slippage_pct"]
    funding = config["backtest"]["funding_rate"]
    leverage = config["trading"]["leverage"]

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
        prev = df.iloc[:i + 1]
        price = float(bar["close"])
        ts = df.index[i]
        atr = float(bar.get("atr_14", 0))
        adx = float(bar.get("adx_14", 0))
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

        signal = strategy.generate_signal(prev, sym)
        if signal != 0 and adx < ADX_MIN:
            if signal == 1 and pos and pos.direction == "short" and pos.amount > 1e-9:
                close_pos(price, "反手")
            elif signal == -1 and pos and pos.direction == "long" and pos.amount > 1e-9:
                close_pos(price, "反手")
            continue

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
    ret = (final - initial) / initial
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
    return {"ret": ret, "sharpe": sharpe, "dd": max_dd, "yearly": yearly, "final": final}


def main():
    data = {sym: load_data(sym) for sym in SYMBOLS}

    print("=" * 115)
    print("Kelly 公式仓位管理回测（MA15/50 + SMA200, 3x, ADX≥15, BTC+LINK+BCH, 2021-2025）")
    print("=" * 115)
    print(f"\n{'方案':<22}{'Return':>10}{'Sharpe':>10}{'MaxDD':>10}{'收益/DD':>10}{'最终权益':>12}{'2021':>8}{'2022':>8}{'2023':>8}{'2024':>8}{'2025':>8}")
    print("-" * 115)

    results = {}
    for label, risk_pct, max_pos in RISK_LEVELS:
        cfg = make_config(risk_pct, max_pos)
        per_sym = {}
        for sym in SYMBOLS:
            eq = run_one(data[sym], sym, cfg)
            per_sym[sym] = eq["equity"]

        port = pd.concat(per_sym.values(), axis=1).ffill().bfill().mean(axis=1)
        s = stats(port)
        rar = s["ret"] / abs(s["dd"]) if s["dd"] != 0 else 0
        y = s["yearly"]
        results[label] = s

        mark = ""
        base = results.get("1.0%(当前)")
        if base and s["sharpe"] > base["sharpe"]:
            mark = " ★"

        print(f"{label:<22}{s['ret']:>+10.1%}{s['sharpe']:>10.2f}{s['dd']:>10.1%}{rar:>10.2f}{s['final']:>12,.0f}{y.get(2021,0):>+8.1%}{y.get(2022,0):>+8.1%}{y.get(2023,0):>+8.1%}{y.get(2024,0):>+8.1%}{y.get(2025,0):>+8.1%}{mark}")

    # 对比
    print(f"\n{'='*115}")
    base = results["1.0%(当前)"]
    print(f"1W USDT 本金 5 年后变成多少（等比缩放）：")
    for label, s in results.items():
        final_1w = s["final"] / 10000 * 10000  # 就是 final 本身
        print(f"  {label}: {final_1w:,.0f} USDT (Δ收益 {s['ret']-base['ret']:+.1%} | ΔSharpe {s['sharpe']-base['sharpe']:+.2f} | ΔDD {s['dd']-base['dd']:+.1%})")


if __name__ == "__main__":
    main()
