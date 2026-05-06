"""
策略改进对比：解决 "长时间不开单" 问题
测试方案：
  A) 基准 MA15/50（仅交叉入场）
  B) 基准 + 回踩重入（趋势确立后回踩慢线反弹入场）
  C) 基准 + Donchian 补充入场（20 bar 新高/新低）
  D) 更快 MA 10/40 + SMA200 + ADX≥15
  E) MA15/50 + 回踩 + Donchian（全部组合）
  F) 基准 + RSI 超卖重入（趋势中 RSI<35 做多，RSI>65 做空）
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
ADX_MIN = 15.0


class SignalEngine:
    """统一信号引擎，支持多种入场方式组合"""

    def __init__(self, fast=15, slow=50, trend=200,
                 use_crossover=True, use_pullback=False,
                 use_donchian=False, use_rsi_reentry=False,
                 donchian_period=20):
        self.fast = fast
        self.slow = slow
        self.trend = trend
        self.use_crossover = use_crossover
        self.use_pullback = use_pullback
        self.use_donchian = use_donchian
        self.use_rsi_reentry = use_rsi_reentry
        self.donchian_period = donchian_period

    def reset(self):
        pass

    def generate_signal(self, df, symbol):
        min_len = max(self.slow, self.trend, self.donchian_period) + 5
        if len(df) < min_len:
            return 0

        close = df["close"]
        price = close.iloc[-1]
        prev_price = close.iloc[-2]

        fast_ma = close.rolling(self.fast).mean()
        slow_ma = close.rolling(self.slow).mean()
        sma200 = df["sma_200"].iloc[-1] if "sma_200" in df.columns else close.rolling(self.trend).mean().iloc[-1]
        adx = df["adx_14"].iloc[-1] if "adx_14" in df.columns else 20

        if pd.isna(fast_ma.iloc[-2]) or pd.isna(slow_ma.iloc[-2]) or pd.isna(sma200):
            return 0

        # 趋势状态
        trend_up = fast_ma.iloc[-1] > slow_ma.iloc[-1] and price > sma200
        trend_down = fast_ma.iloc[-1] < slow_ma.iloc[-1] and price < sma200

        signal = 0

        # 1) 交叉信号（原始）
        if self.use_crossover:
            if fast_ma.iloc[-2] <= slow_ma.iloc[-2] and fast_ma.iloc[-1] > slow_ma.iloc[-1]:
                if price > sma200:
                    signal = 1
            if fast_ma.iloc[-2] >= slow_ma.iloc[-2] and fast_ma.iloc[-1] < slow_ma.iloc[-1]:
                if price < sma200:
                    signal = -1

        # 2) 回踩重入：趋势中价格回踩慢线后反弹
        if self.use_pullback and signal == 0:
            slow_val = slow_ma.iloc[-1]
            prev_slow = slow_ma.iloc[-2]
            if trend_up and adx >= ADX_MIN:
                # 上一根触及或跌破慢线，这一根收回慢线上方
                if prev_price <= prev_slow * 1.005 and price > slow_val:
                    signal = 1
            if trend_down and adx >= ADX_MIN:
                if prev_price >= prev_slow * 0.995 and price < slow_val:
                    signal = -1

        # 3) Donchian 突破补充
        if self.use_donchian and signal == 0:
            period = self.donchian_period
            highs = df["high"].iloc[-(period + 1):-1]
            lows = df["low"].iloc[-(period + 1):-1]
            upper = highs.max()
            lower = lows.min()
            # 只在趋势方向上突破
            if trend_up and price > upper and prev_price <= upper:
                signal = 1
            elif trend_down and price < lower and prev_price >= lower:
                signal = -1

        # 4) RSI 超卖/超买重入
        if self.use_rsi_reentry and signal == 0:
            rsi = df["rsi_14"].iloc[-1] if "rsi_14" in df.columns else None
            prev_rsi = df["rsi_14"].iloc[-2] if "rsi_14" in df.columns else None
            if rsi is not None and not pd.isna(rsi) and not pd.isna(prev_rsi):
                if trend_up and prev_rsi < 35 and rsi >= 35:
                    signal = 1
                elif trend_down and prev_rsi > 65 and rsi <= 65:
                    signal = -1

        return signal


STRATEGIES = [
    ("A) 基准 MA15/50",
     SignalEngine(15, 50, 200, use_crossover=True)),
    ("B) +回踩重入",
     SignalEngine(15, 50, 200, use_crossover=True, use_pullback=True)),
    ("C) +Donchian补充",
     SignalEngine(15, 50, 200, use_crossover=True, use_donchian=True)),
    ("D) 快MA 10/40",
     SignalEngine(10, 40, 200, use_crossover=True)),
    ("E) +回踩+Donchian",
     SignalEngine(15, 50, 200, use_crossover=True, use_pullback=True, use_donchian=True)),
    ("F) +RSI重入",
     SignalEngine(15, 50, 200, use_crossover=True, use_rsi_reentry=True)),
    ("G) 全组合(回踩+DC+RSI)",
     SignalEngine(15, 50, 200, use_crossover=True, use_pullback=True, use_donchian=True, use_rsi_reentry=True)),
    ("H) 快MA10/40+回踩",
     SignalEngine(10, 40, 200, use_crossover=True, use_pullback=True)),
]

RISK_LEVELS = [
    ("1.5%", 0.015, 0.25),
    ("2.7%", 0.027, 0.35),
    ("3.5%", 0.035, 0.40),
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
    return add_indicators(pd.read_parquet(f"data/{safe}_4h_2021-01-01_2025-12-31.parquet"))


def run_backtest(df, sym, config, engine):
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
    signal_count = 0

    for i in range(1, len(df)):
        bar = df.iloc[i]
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
                trade_count += 1
                continue
            elif pos.direction == "short" and bar_high >= stop_price:
                close_pos(stop_price, "止损")
                trade_count += 1
                continue
            if risk.check_take_profit(pos.avg_price, price, pos.direction, sym):
                close_pos(price, "止盈")
                trade_count += 1
                continue

        prev_df = df.iloc[:i + 1]
        signal = engine.generate_signal(prev_df, sym)

        if signal != 0 and adx < ADX_MIN:
            if signal == 1 and pos and pos.direction == "short" and pos.amount > 1e-9:
                close_pos(price, "反手")
                trade_count += 1
            elif signal == -1 and pos and pos.direction == "long" and pos.amount > 1e-9:
                close_pos(price, "反手")
                trade_count += 1
            continue

        if signal != 0:
            signal_count += 1

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

    pos = portfolio.get_position(sym)
    if pos and pos.amount > 1e-9:
        close_pos(float(df.iloc[-1]["close"]), "回测结束")
        trade_count += 1

    eq_df = pd.DataFrame(portfolio.equity_curve).set_index("timestamp")
    return eq_df, trade_count, signal_count


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

    print("=" * 145)
    print('策略入场方式对比（解决"长时间不开单"问题）')
    print("3x 杠杆 | BTC+LINK+BCH | 2021-2025 | 4h | ATR止损 | 6%止盈")
    print("=" * 145)

    all_results = {}

    for strat_name, engine in STRATEGIES:
        print(f"\n{'─' * 145}")
        print(f"  {strat_name}")
        print(f"{'─' * 145}")
        print(f"  {'Kelly':<10}{'Return':>10}{'Sharpe':>10}{'MaxDD':>10}{'收益/DD':>10}{'信号数':>8}{'交易数':>8}{'最终权益':>12}{'2021':>8}{'2022':>8}{'2023':>8}{'2024':>8}{'2025':>8}")

        for kelly_label, risk_pct, max_pos in RISK_LEVELS:
            cfg = make_config(risk_pct, max_pos)
            per_sym = {}
            total_trades = 0
            total_signals = 0
            for sym in SYMBOLS:
                engine.reset()
                eq, trades, sigs = run_backtest(data[sym], sym, cfg, engine)
                per_sym[sym] = eq["equity"]
                total_trades += trades
                total_signals += sigs

            port = pd.concat(per_sym.values(), axis=1).ffill().bfill().mean(axis=1)
            s = calc_stats(port)
            rar = s["ret"] / abs(s["dd"]) if s["dd"] != 0 else 0
            y = s["yearly"]
            key = f"{strat_name}|{kelly_label}"
            all_results[key] = {**s, "trades": total_trades, "signals": total_signals, "rar": rar}

            print(f"  {kelly_label:<10}{s['ret']:>+10.1%}{s['sharpe']:>10.2f}{s['dd']:>10.1%}{rar:>10.2f}{total_signals:>8}{total_trades:>8}{s['final']:>12,.0f}{y.get(2021,0):>+8.1%}{y.get(2022,0):>+8.1%}{y.get(2023,0):>+8.1%}{y.get(2024,0):>+8.1%}{y.get(2025,0):>+8.1%}")

    # 排行榜
    print(f"\n{'=' * 145}")
    print("Top 12（按 Sharpe 排序）")
    print(f"{'=' * 145}")
    print(f"  {'#':<4}{'组合':<45}{'Return':>10}{'Sharpe':>10}{'MaxDD':>10}{'收益/DD':>10}{'信号':>6}{'交易':>6}")
    print(f"  {'-' * 101}")

    ranked = sorted(all_results.items(), key=lambda x: x[1]["sharpe"], reverse=True)
    for i, (key, s) in enumerate(ranked[:12], 1):
        print(f"  {i:<4}{key:<45}{s['ret']:>+10.1%}{s['sharpe']:>10.2f}{s['dd']:>10.1%}{s['rar']:>10.2f}{s['signals']:>6}{s['trades']:>6}")

    # 信号频率对比
    print(f"\n{'=' * 145}")
    print("信号频率对比（1.5% Kelly）")
    print(f"{'=' * 145}")
    baseline_sigs = None
    for key, s in all_results.items():
        if "|1.5%" in key:
            label = key.split("|")[0]
            if baseline_sigs is None:
                baseline_sigs = s["signals"]
            ratio = s["signals"] / baseline_sigs if baseline_sigs else 0
            avg_gap = 5 * 365 * 6 / s["signals"] if s["signals"] > 0 else 9999  # 平均多少根K线一个信号
            avg_gap_days = avg_gap * 4 / 24  # 转天
            print(f"  {label:<35} 信号:{s['signals']:>5} | 交易:{s['trades']:>5} | 是基准的 {ratio:>5.1f}x | 平均每 {avg_gap_days:>5.1f} 天一个信号")


if __name__ == "__main__":
    main()
