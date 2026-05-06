"""
激进策略 × Kelly 比例回测对比
策略：
  A) 快速 MA (5/20, 无趋势过滤) — 高频捕捉短趋势
  B) MACD 动量 — MACD 柱翻正做多、翻负做空
  C) 布林带突破 — 突破上轨做多、突破下轨做空（趋势追踪）
  D) RSI 极端反转 — RSI<25 做多、RSI>75 做空
  E) Donchian 通道突破 — 20 bar 新高做多、新低做空
  F) 当前基准 (MA15/50 + SMA200 + ADX≥15)
"""
import os, sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.data_feed import add_indicators
from core.portfolio import Portfolio
from core.risk import RiskManager
from core.order import Order, OrderSide, OrderType

SYMBOLS = ["BTC/USDT:USDT", "LINK/USDT:USDT", "BCH/USDT:USDT"]

KELLY_LEVELS = [
    ("1.5%", 0.015, 0.25),
    ("2.7%(1/4K)", 0.027, 0.35),
    ("3.5%", 0.035, 0.40),
    ("5.5%(半K)", 0.055, 0.50),
]


# ── 策略信号函数 ──────────────────────────────────────────

def signal_fast_ma(df, _sym):
    """A) 快速 MA 5/20，无趋势过滤"""
    if len(df) < 22:
        return 0
    close = df["close"]
    fast = close.rolling(5).mean()
    slow = close.rolling(20).mean()
    if pd.isna(fast.iloc[-2]) or pd.isna(slow.iloc[-2]):
        return 0
    # 金叉
    if fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]:
        return 1
    # 死叉
    if fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]:
        return -1
    return 0


def signal_macd(df, _sym):
    """B) MACD 动量：柱状图从负翻正做多，从正翻负做空"""
    if len(df) < 35:
        return 0
    hist = df["macd_hist"] if "macd_hist" in df.columns else None
    if hist is None:
        return 0
    prev, curr = hist.iloc[-2], hist.iloc[-1]
    if pd.isna(prev) or pd.isna(curr):
        return 0
    if prev <= 0 and curr > 0:
        return 1
    if prev >= 0 and curr < 0:
        return -1
    return 0


def signal_bb_breakout(df, _sym):
    """C) 布林带趋势突破：收盘突破上轨做多，突破下轨做空（与均值回归相反）"""
    if len(df) < 22:
        return 0
    close = df["close"].iloc[-1]
    prev_close = df["close"].iloc[-2]
    upper = df["bb_upper"].iloc[-1] if "bb_upper" in df.columns else None
    lower = df["bb_lower"].iloc[-1] if "bb_lower" in df.columns else None
    if upper is None or pd.isna(upper):
        return 0
    prev_upper = df["bb_upper"].iloc[-2]
    prev_lower = df["bb_lower"].iloc[-2]
    # 突破上轨 → 做多
    if prev_close <= prev_upper and close > upper:
        return 1
    # 突破下轨 → 做空
    if prev_close >= prev_lower and close < lower:
        return -1
    return 0


def signal_rsi_extreme(df, _sym):
    """D) RSI 极端反转：RSI<25 做多，RSI>75 做空"""
    if len(df) < 16:
        return 0
    rsi = df["rsi_14"].iloc[-1] if "rsi_14" in df.columns else None
    prev_rsi = df["rsi_14"].iloc[-2] if "rsi_14" in df.columns else None
    if rsi is None or pd.isna(rsi) or pd.isna(prev_rsi):
        return 0
    # RSI 从超卖区回升 → 做多
    if prev_rsi < 25 and rsi >= 25:
        return 1
    # RSI 从超买区回落 → 做空
    if prev_rsi > 75 and rsi <= 75:
        return -1
    return 0


def signal_donchian(df, _sym):
    """E) Donchian 通道 20 bar 突破"""
    period = 20
    if len(df) < period + 2:
        return 0
    highs = df["high"].iloc[-(period + 1):-1]
    lows = df["low"].iloc[-(period + 1):-1]
    upper = highs.max()
    lower = lows.min()
    close = df["close"].iloc[-1]
    prev_close = df["close"].iloc[-2]
    if prev_close <= upper and close > upper:
        return 1
    if prev_close >= lower and close < lower:
        return -1
    return 0


def signal_baseline(df, _sym):
    """F) 当前策略：MA15/50 + SMA200 + ADX≥15"""
    if len(df) < 202:
        return 0
    close = df["close"]
    fast = close.rolling(15).mean()
    slow = close.rolling(50).mean()
    sma200 = df["sma_200"].iloc[-1] if "sma_200" in df.columns else close.rolling(200).mean().iloc[-1]
    adx = df["adx_14"].iloc[-1] if "adx_14" in df.columns else 20
    if pd.isna(fast.iloc[-2]) or pd.isna(slow.iloc[-2]) or pd.isna(sma200):
        return 0
    if adx < 15:
        return 0
    price = close.iloc[-1]
    if fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]:
        if price > sma200:
            return 1
    if fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]:
        if price < sma200:
            return -1
    return 0


STRATEGIES = [
    ("A) 快MA 5/20", signal_fast_ma),
    ("B) MACD动量", signal_macd),
    ("C) BB突破", signal_bb_breakout),
    ("D) RSI极端", signal_rsi_extreme),
    ("E) Donchian20", signal_donchian),
    ("F) 基准MA15/50", signal_baseline),
]


# ── 回测引擎 ──────────────────────────────────────────

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
    return add_indicators(pd.read_parquet(f"data/{safe}_4h_2021-01-01_2025-12-31.parquet"))


def run_backtest(df, sym, config, signal_fn):
    portfolio = Portfolio(config["backtest"]["initial_capital"])
    risk = RiskManager(config)
    risk.reset()

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

    trade_count = 0
    win_count = 0

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
            hit_stop = False
            if pos.direction == "long" and bar_low <= stop_price:
                close_pos(stop_price, "止损")
                hit_stop = True
            elif pos.direction == "short" and bar_high >= stop_price:
                close_pos(stop_price, "止损")
                hit_stop = True
            if hit_stop:
                trade_count += 1
                continue
            if risk.check_take_profit(pos.avg_price, price, pos.direction, sym):
                close_pos(price, "止盈")
                trade_count += 1
                win_count += 1
                continue

        prev_df = df.iloc[:i + 1]
        signal = signal_fn(prev_df, sym)

        if signal == 1:
            if pos and pos.direction == "short" and pos.amount > 1e-9:
                close_pos(price, "反手")
                trade_count += 1
            if not portfolio.get_position(sym):
                open_pos("long", price, equity, atr)
        elif signal == -1:
            if pos and pos.direction == "long" and pos.amount > 1e-9:
                close_pos(price, "反手")
                trade_count += 1
            if not portfolio.get_position(sym):
                open_pos("short", price, equity, atr)

    # 结束
    pos = portfolio.get_position(sym)
    if pos and pos.amount > 1e-9:
        close_pos(float(df.iloc[-1]["close"]), "回测结束")
        trade_count += 1

    eq_df = pd.DataFrame(portfolio.equity_curve).set_index("timestamp")
    return eq_df, trade_count, win_count


def calc_stats(eq, initial=10000):
    final = float(eq.iloc[-1])
    ret = (final - initial) / initial
    rets = eq.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(365 * 6)) if rets.std() > 0 else 0
    rolling_max = eq.cummax()
    dd = (eq - rolling_max) / rolling_max
    max_dd = float(dd.min())
    eq.index = pd.to_datetime(eq.index)
    yearly = {}
    for year in range(2021, 2026):
        yr = eq[eq.index.year == year]
        if len(yr) > 1:
            yearly[year] = (yr.iloc[-1] - yr.iloc[0]) / yr.iloc[0]
    return {"ret": ret, "sharpe": sharpe, "dd": max_dd, "yearly": yearly, "final": final}


def main():
    data = {sym: load_data(sym) for sym in SYMBOLS}

    print("=" * 130)
    print("激进策略 × Kelly 比例回测（3x 杠杆, BTC+LINK+BCH, 2021-2025, 4h）")
    print("=" * 130)

    all_results = {}

    for strat_name, signal_fn in STRATEGIES:
        print(f"\n{'─' * 130}")
        print(f"  {strat_name}")
        print(f"{'─' * 130}")
        print(f"  {'Kelly':<16}{'Return':>10}{'Sharpe':>10}{'MaxDD':>10}{'收益/DD':>10}{'交易数':>8}{'最终权益':>12}{'2021':>8}{'2022':>8}{'2023':>8}{'2024':>8}{'2025':>8}")

        for kelly_label, risk_pct, max_pos in KELLY_LEVELS:
            cfg = make_config(risk_pct, max_pos)
            per_sym = {}
            total_trades = 0
            total_wins = 0
            for sym in SYMBOLS:
                eq, trades, wins = run_backtest(data[sym], sym, cfg, signal_fn)
                per_sym[sym] = eq["equity"]
                total_trades += trades
                total_wins += wins

            port = pd.concat(per_sym.values(), axis=1).ffill().bfill().mean(axis=1)
            s = calc_stats(port)
            rar = s["ret"] / abs(s["dd"]) if s["dd"] != 0 else 0
            y = s["yearly"]
            key = f"{strat_name}|{kelly_label}"
            all_results[key] = {**s, "trades": total_trades, "rar": rar}

            print(f"  {kelly_label:<16}{s['ret']:>+10.1%}{s['sharpe']:>10.2f}{s['dd']:>10.1%}{rar:>10.2f}{total_trades:>8}{s['final']:>12,.0f}{y.get(2021,0):>+8.1%}{y.get(2022,0):>+8.1%}{y.get(2023,0):>+8.1%}{y.get(2024,0):>+8.1%}{y.get(2025,0):>+8.1%}")

    # ── 排行榜 ──
    print(f"\n{'=' * 130}")
    print("Top 10 组合（按 Sharpe 排序）")
    print(f"{'=' * 130}")
    print(f"  {'#':<4}{'组合':<35}{'Return':>10}{'Sharpe':>10}{'MaxDD':>10}{'收益/DD':>10}{'交易数':>8}")
    print(f"  {'-' * 87}")

    ranked = sorted(all_results.items(), key=lambda x: x[1]["sharpe"], reverse=True)
    for i, (key, s) in enumerate(ranked[:10], 1):
        print(f"  {i:<4}{key:<35}{s['ret']:>+10.1%}{s['sharpe']:>10.2f}{s['dd']:>10.1%}{s['rar']:>10.2f}{s['trades']:>8}")

    print(f"\nTop 10 组合（按绝对收益排序）")
    print(f"  {'#':<4}{'组合':<35}{'Return':>10}{'Sharpe':>10}{'MaxDD':>10}{'收益/DD':>10}{'交易数':>8}")
    print(f"  {'-' * 87}")
    ranked_ret = sorted(all_results.items(), key=lambda x: x[1]["ret"], reverse=True)
    for i, (key, s) in enumerate(ranked_ret[:10], 1):
        print(f"  {i:<4}{key:<35}{s['ret']:>+10.1%}{s['sharpe']:>10.2f}{s['dd']:>10.1%}{s['rar']:>10.2f}{s['trades']:>8}")


if __name__ == "__main__":
    main()
