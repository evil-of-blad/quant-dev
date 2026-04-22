"""
在 MA15/50 + SMA200 基础上加波动率优化
A. 当前 baseline（固定 6% 止盈 + ATR 3.0 止损）
B. 波动率自适应止盈止损（高波动放宽，低波动收紧）
C. 低波动期才开仓（ATR < 中位数时开仓，避免在高波动期追高）
D. 高波动期加宽止盈（ATR 高时止盈从 6% 放到 10%）
E. 波动率确认开仓（金叉后等 ATR 收缩再进场）
F. 综合：自适应止盈 + 低波动开仓
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


def make_config(atr_mult=3.0, tp=0.06):
    return {
        "trading": {"leverage": 3, "timeframe": "4h"},
        "backtest": {"initial_capital": 10000.0, "fee_rate": 0.0005, "slippage_pct": 0.0002, "funding_rate": 0.0001},
        "risk": {
            "max_position_pct": 0.2, "stop_loss_pct": 0.03, "take_profit_pct": tp,
            "max_drawdown_pct": 0.15, "risk_per_trade_pct": 0.01, "cooldown_bars": 100,
            "use_atr_stop": True, "atr_stop_mult": atr_mult, "use_trailing_stop": False,
        },
    }


def load_data(sym):
    safe = sym.replace("/", "_")
    df = pd.read_parquet(f"data/{safe}_4h_2021-01-01_2025-12-31.parquet")
    df = add_indicators(df)
    # ATR 百分比（归一化）
    df["atr_pct"] = df["atr_14"] / df["close"] * 100
    # ATR 的滚动中位数（用扩展窗口避免前视）
    df["atr_pct_median"] = df["atr_pct"].expanding(min_periods=180).median()
    # ATR 相对中位数的比值
    df["atr_ratio"] = df["atr_pct"] / df["atr_pct_median"]
    df.dropna(subset=["atr_ratio"], inplace=True)
    return df


def run_one(df, sym, mode):
    config = make_config()
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
        atr_ratio = float(bar.get("atr_ratio", 1.0))
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

        # 动态止盈止损参数
        if mode == "B":
            # 高波动放宽，低波动收紧
            dynamic_tp = 0.06 * max(0.8, min(atr_ratio, 2.0))
            dynamic_atr_mult = 3.0 * max(0.8, min(atr_ratio, 1.5))
        elif mode == "D":
            # 高波动时放宽止盈
            dynamic_tp = 0.06 if atr_ratio < 1.2 else 0.10 if atr_ratio < 1.5 else 0.15
            dynamic_atr_mult = 3.0
        elif mode == "F":
            dynamic_tp = 0.06 * max(0.8, min(atr_ratio, 2.0))
            dynamic_atr_mult = 3.0 * max(0.8, min(atr_ratio, 1.5))
        else:
            dynamic_tp = 0.06
            dynamic_atr_mult = 3.0

        if pos and pos.amount > 1e-9:
            bar_high = float(bar.get("high", price))
            bar_low = float(bar.get("low", price))
            stop_price = risk.calc_stop_price(pos.avg_price, pos.direction, atr * dynamic_atr_mult / 3.0)

            if pos.direction == "long" and bar_low <= stop_price:
                close_pos(stop_price, "止损")
                continue
            elif pos.direction == "short" and bar_high >= stop_price:
                close_pos(stop_price, "止损")
                continue

            # 动态止盈
            if pos.direction == "long":
                tp_price = pos.avg_price * (1 + dynamic_tp)
                if price >= tp_price:
                    close_pos(price, "止盈")
                    continue
            else:
                tp_price = pos.avg_price * (1 - dynamic_tp)
                if price <= tp_price:
                    close_pos(price, "止盈")
                    continue

        signal = strategy.generate_signal(prev, sym)

        # 开仓过滤
        if mode == "C" or mode == "F":
            # 只在低波动期开仓（ATR < 1.2 倍中位数）
            if signal != 0 and atr_ratio > 1.2:
                # 高波动，跳过开仓（但仍然平反向仓位）
                if signal == 1 and pos and pos.direction == "short" and pos.amount > 1e-9:
                    close_pos(price, "反手")
                elif signal == -1 and pos and pos.direction == "long" and pos.amount > 1e-9:
                    close_pos(price, "反手")
                continue
        elif mode == "E":
            # 等 ATR 收缩确认（ATR 下降趋势才开仓）
            if signal != 0 and i >= 3:
                atr_prev = float(df["atr_pct"].iloc[i - 3])
                atr_now = float(df["atr_pct"].iloc[i])
                if atr_now > atr_prev:  # ATR 在扩张，不开仓
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
    return {"ret": ret, "sharpe": sharpe, "dd": max_dd}


def main():
    data = {sym: load_data(sym) for sym in SYMBOLS}

    modes = [
        ("A.当前baseline", "A"),
        ("B.自适应止盈止损", "B"),
        ("C.低波动才开仓", "C"),
        ("D.高波动放宽止盈", "D"),
        ("E.ATR收缩确认开仓", "E"),
        ("F.综合(B+C)", "F"),
    ]

    print("=" * 80)
    print("波动率优化回测（MA15/50 + SMA200, 3x, BTC+LINK+BCH, 2021-2025）")
    print("=" * 80)
    print(f"\n{'方案':<22}{'Return':>10}{'Sharpe':>10}{'MaxDD':>10}{'收益/DD':>10}")
    print("-" * 65)

    results = {}
    for label, mode in modes:
        per_sym = {}
        for sym in SYMBOLS:
            eq = run_one(data[sym], sym, mode)
            per_sym[sym] = eq["equity"]
        port = pd.concat(per_sym.values(), axis=1).ffill().bfill().mean(axis=1)
        s = stats(port)
        rar = s["ret"] / abs(s["dd"]) if s["dd"] != 0 else 0
        results[label] = s
        mark = " ★" if s["sharpe"] > results.get("A.当前baseline", {"sharpe": 0})["sharpe"] else ""
        print(f"{label:<22}{s['ret']:>+10.1%}{s['sharpe']:>10.2f}{s['dd']:>10.1%}{rar:>10.2f}{mark}")

    base = results["A.当前baseline"]
    print(f"\nvs baseline 对比:")
    for label, s in results.items():
        if label.startswith("A."):
            continue
        print(f"  {label}: Δ收益 {s['ret']-base['ret']:+.1%} | ΔSharpe {s['sharpe']-base['sharpe']:+.2f} | ΔDD {s['dd']-base['dd']:+.1%}")


if __name__ == "__main__":
    main()
