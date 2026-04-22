"""
止盈后重新入场回测
A. 当前：止盈后等下次交叉才能重新开仓
B. 止盈重入：止盈后如果 MA 状态仍满足，下一根 K 线重新开仓
C. 止盈重入 + 冷却：止盈后等 3 根 K 线（12h）再检查重入
D. 无止盈（对照）
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

def make_config():
    return {
        "trading": {"leverage": 3, "timeframe": "4h"},
        "backtest": {"initial_capital": 10000.0, "fee_rate": 0.0005, "slippage_pct": 0.0002, "funding_rate": 0.0001},
        "risk": {
            "max_position_pct": 0.2, "stop_loss_pct": 0.03, "take_profit_pct": 0.06,
            "max_drawdown_pct": 0.15, "risk_per_trade_pct": 0.01, "cooldown_bars": 100,
            "use_atr_stop": True, "atr_stop_mult": 3.0, "use_trailing_stop": False,
        },
    }


def load_data(sym):
    safe = sym.replace("/", "_")
    df = pd.read_parquet(f"data/{safe}_4h_2021-01-01_2025-12-31.parquet")
    return add_indicators(df)


def check_ma_state(df, i, fast_p=15, slow_p=50, trend_p=200):
    """检查第 i 根 bar 的 MA 状态，返回 'long' / 'short' / None"""
    if i < trend_p:
        return None
    fast = df["close"].rolling(fast_p).mean().iloc[i]
    slow = df["close"].rolling(slow_p).mean().iloc[i]
    sma200 = df["sma_200"].iloc[i] if "sma_200" in df.columns else df["close"].rolling(trend_p).mean().iloc[i]
    price = df["close"].iloc[i]

    if fast != fast or slow != slow or sma200 != sma200:
        return None
    if fast > slow and price > sma200:
        return "long"
    if fast < slow and price < sma200:
        return "short"
    return None


def run_one(df, sym, mode):
    """
    mode: 'baseline' / 'reentry' / 'reentry_cool' / 'no_tp'
    """
    config = make_config()
    if mode == "no_tp":
        config["risk"]["take_profit_pct"] = 9.99

    portfolio = Portfolio(config["backtest"]["initial_capital"])
    risk = RiskManager(config)
    risk.reset()
    strategy = MACrossoverStrategy(PARAMS)
    strategy.reset()

    fee_rate = config["backtest"]["fee_rate"]
    slip = config["backtest"]["slippage_pct"]
    funding = config["backtest"]["funding_rate"]
    leverage = config["trading"]["leverage"]

    cooldown_after_tp = 0  # 止盈后冷却计数
    COOL_BARS = 3 if mode == "reentry_cool" else 0
    tp_count = 0
    reentry_count = 0

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
        equity = portfolio.snapshot(ts, {sym: price})

        if portfolio.check_liquidation(sym, price):
            risk.reset_trailing(sym)
            continue
        if i % 2 == 0:
            portfolio.deduct_funding_rate(sym, price, funding)
        if risk.check_drawdown(equity):
            close_pos(price, "熔断")
            cooldown_after_tp = 0
            continue

        # 冷却倒计时
        if cooldown_after_tp > 0:
            cooldown_after_tp -= 1

        pos = portfolio.get_position(sym)
        if pos and pos.amount > 1e-9:
            # 止损（high/low）
            bar_high = float(bar.get("high", price))
            bar_low = float(bar.get("low", price))
            stop_price = risk.calc_stop_price(pos.avg_price, pos.direction, atr)
            if pos.direction == "long" and bar_low <= stop_price:
                close_pos(stop_price, "止损")
                cooldown_after_tp = 0
                continue
            elif pos.direction == "short" and bar_high >= stop_price:
                close_pos(stop_price, "止损")
                cooldown_after_tp = 0
                continue

            # 止盈
            if risk.check_take_profit(pos.avg_price, price, pos.direction, sym):
                close_pos(price, "止盈")
                tp_count += 1
                if mode in ("reentry", "reentry_cool"):
                    cooldown_after_tp = COOL_BARS + 1  # +1 因为当前 bar 不重入
                continue

        # 策略信号（正常交叉）
        signal = strategy.generate_signal(prev, sym)
        if signal == 1:
            if pos and pos.direction == "short" and pos.amount > 1e-9:
                close_pos(price, "反手")
                cooldown_after_tp = 0
            if not portfolio.get_position(sym):
                open_pos("long", price, equity, atr)
                cooldown_after_tp = 0
        elif signal == -1:
            if pos and pos.direction == "long" and pos.amount > 1e-9:
                close_pos(price, "反手")
                cooldown_after_tp = 0
            if not portfolio.get_position(sym):
                open_pos("short", price, equity, atr)
                cooldown_after_tp = 0
        else:
            # 无交叉信号 → 检查止盈重入
            if mode in ("reentry", "reentry_cool") and not portfolio.get_position(sym) and cooldown_after_tp == 0:
                ma_state = check_ma_state(df, i)
                if ma_state is not None:
                    open_pos(ma_state, price, equity, atr)
                    reentry_count += 1

    close_pos(float(df.iloc[-1]["close"]), "回测结束")
    eq_df = pd.DataFrame(portfolio.equity_curve).set_index("timestamp")
    return eq_df, tp_count, reentry_count


def stats(eq, initial=10000):
    final = float(eq.iloc[-1])
    total_ret = (final - initial) / initial
    rets = eq.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(365 * 6)) if rets.std() > 0 else 0
    rolling_max = eq.cummax()
    dd = (eq - rolling_max) / rolling_max
    max_dd = float(dd.min())
    return {"ret": total_ret, "sharpe": sharpe, "dd": max_dd}


def main():
    data = {sym: load_data(sym) for sym in SYMBOLS}

    modes = [
        ("A.当前(止盈不重入)", "baseline"),
        ("B.止盈后立刻重入", "reentry"),
        ("C.止盈后冷却12h重入", "reentry_cool"),
        ("D.无止盈(对照)", "no_tp"),
    ]

    print("=" * 90)
    print("止盈后重新入场回测（MA15/50 + SMA200, 3x, BTC+LINK+BCH, 2021-2025）")
    print("=" * 90)

    results = {}
    for label, mode in modes:
        per_sym = {}
        total_tp = 0
        total_reentry = 0
        for sym in SYMBOLS:
            eq, tp, re = run_one(data[sym], sym, mode)
            per_sym[sym] = eq["equity"]
            total_tp += tp
            total_reentry += re

        port = pd.concat(per_sym.values(), axis=1).ffill().bfill().mean(axis=1)
        s = stats(port)
        s["tp_count"] = total_tp
        s["reentry_count"] = total_reentry
        results[label] = s

    print(f"\n{'方案':<24}{'Return':>10}{'Sharpe':>10}{'MaxDD':>10}{'收益/DD':>10}{'止盈次':>8}{'重入次':>8}")
    print("-" * 82)
    for label, s in results.items():
        rar = s["ret"] / abs(s["dd"]) if s["dd"] != 0 else 0
        print(f"{label:<24}{s['ret']:>+10.1%}{s['sharpe']:>10.2f}{s['dd']:>10.1%}{rar:>10.2f}{s['tp_count']:>8}{s['reentry_count']:>8}")

    # 对比
    print(f"\n{'='*90}")
    base = results["A.当前(止盈不重入)"]
    for label, s in results.items():
        if label.startswith("A."):
            continue
        print(f"{label} vs 当前:")
        print(f"  Return {base['ret']:+.1%} → {s['ret']:+.1%} (Δ {s['ret']-base['ret']:+.1%})")
        print(f"  Sharpe {base['sharpe']:.2f} → {s['sharpe']:.2f} (Δ {s['sharpe']-base['sharpe']:+.2f})")
        print(f"  MaxDD  {base['dd']:.1%} → {s['dd']:.1%} (Δ {s['dd']-base['dd']:+.1%})")
        print()


if __name__ == "__main__":
    main()
