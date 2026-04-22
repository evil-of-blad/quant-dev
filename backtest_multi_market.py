"""
多市场 MA 趋势策略回测
- 商品期货：黄金、原油、铜、白银、天然气
- 美股指数：SPY (标普500)、QQQ (纳斯达克)、IWM (罗素2000)
- 数据源：Yahoo Finance（日线）
- 对比加密币 4h → 日线参数需要调整

加密币 4h MA15/50 ≈ 日线 MA3/10（太短，噪音大）
所以日线上测 5/20、10/30、15/50、20/60 几组参数
SMA200 趋势过滤保留（200 日均线是股票/商品市场的经典指标）
"""
import numpy as np
import pandas as pd
import yfinance as yf
from dataclasses import dataclass

# 标的列表
ASSETS = {
    # 商品期货
    "黄金": "GC=F",
    "原油": "CL=F",
    "铜": "HG=F",
    "白银": "SI=F",
    "天然气": "NG=F",
    # 美股指数 ETF
    "SPY(标普)": "SPY",
    "QQQ(纳指)": "QQQ",
    "IWM(罗素)": "IWM",
    # 对照组：加密币（日线）
    "BTC": "BTC-USD",
}

MA_PARAMS = [
    {"name": "5/20", "fast": 5, "slow": 20},
    {"name": "10/30", "fast": 10, "slow": 30},
    {"name": "15/50", "fast": 15, "slow": 50},
    {"name": "20/60", "fast": 20, "slow": 60},
]

START = "2019-01-01"
END = "2025-12-31"
INITIAL = 10000
FEE_RATE = 0.0002  # 期货/股票手续费远低于加密币
TREND_PERIOD = 200


@dataclass
class Trade:
    entry_time: str
    exit_time: str
    direction: str
    entry_price: float
    exit_price: float
    pnl: float
    days: float


def fetch_data(ticker: str) -> pd.DataFrame:
    """拉取日线数据"""
    df = yf.download(ticker, start=START, end=END, progress=False, auto_adjust=True)
    if df.empty:
        return None
    # 统一列名
    df.columns = [c.lower() if isinstance(c, str) else c[0].lower() for c in df.columns]
    df = df[["open", "high", "low", "close", "volume"]].copy()
    df.dropna(inplace=True)
    return df


def add_ma(df, fast, slow, trend=200):
    df = df.copy()
    df["sma_fast"] = df["close"].rolling(fast).mean()
    df["sma_slow"] = df["close"].rolling(slow).mean()
    df["sma_trend"] = df["close"].rolling(trend).mean()

    # ATR
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"] - df["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()

    df.dropna(inplace=True)
    return df


def run_backtest(df, fast, slow, use_leverage=False):
    """
    简化回测，无杠杆（期货/股票用原始收益衡量，不加杠杆对比更公平）
    如果 use_leverage=True，用 3x（和加密币对比用）
    """
    lev = 3 if use_leverage else 1
    capital = INITIAL
    position = 0  # 0=空仓, 1=多, -1=空
    entry_price = 0
    entry_time = None
    trades = []
    equity_curve = [capital]

    atr_mult = 3.0  # 和加密币一样

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        price = float(row["close"])
        atr = float(row["atr"])
        high = float(row["high"])
        low = float(row["low"])

        # 盘中止损检查
        if position != 0 and atr > 0:
            if position == 1:
                stop = entry_price - atr * atr_mult
                if low <= stop:
                    pnl = (stop - entry_price) / entry_price * capital * lev
                    pnl -= capital * FEE_RATE * 2  # 开平手续费
                    capital += pnl
                    days = (df.index[i] - entry_time).days if entry_time else 0
                    trades.append(Trade(str(entry_time)[:10], str(df.index[i])[:10], "多", entry_price, stop, pnl, days))
                    position = 0
                    equity_curve.append(capital)
                    continue
            elif position == -1:
                stop = entry_price + atr * atr_mult
                if high >= stop:
                    pnl = (entry_price - stop) / entry_price * capital * lev
                    pnl -= capital * FEE_RATE * 2
                    capital += pnl
                    days = (df.index[i] - entry_time).days if entry_time else 0
                    trades.append(Trade(str(entry_time)[:10], str(df.index[i])[:10], "空", entry_price, stop, pnl, days))
                    position = 0
                    equity_curve.append(capital)
                    continue

        # 信号（MA 交叉 + SMA200 过滤）
        prev_fast = float(prev["sma_fast"])
        prev_slow = float(prev["sma_slow"])
        curr_fast = float(row["sma_fast"])
        curr_slow = float(row["sma_slow"])
        trend = float(row["sma_trend"])

        signal = 0
        if prev_fast <= prev_slow and curr_fast > curr_slow and price > trend:
            signal = 1
        elif prev_fast >= prev_slow and curr_fast < curr_slow and price < trend:
            signal = -1

        if signal == 1 and position != 1:
            if position == -1:
                pnl = (entry_price - price) / entry_price * capital * lev
                pnl -= capital * FEE_RATE * 2
                capital += pnl
                days = (df.index[i] - entry_time).days if entry_time else 0
                trades.append(Trade(str(entry_time)[:10], str(df.index[i])[:10], "空", entry_price, price, pnl, days))
            position = 1
            entry_price = price
            entry_time = df.index[i]

        elif signal == -1 and position != -1:
            if position == 1:
                pnl = (price - entry_price) / entry_price * capital * lev
                pnl -= capital * FEE_RATE * 2
                capital += pnl
                days = (df.index[i] - entry_time).days if entry_time else 0
                trades.append(Trade(str(entry_time)[:10], str(df.index[i])[:10], "多", entry_price, price, pnl, days))
            position = -1
            entry_price = price
            entry_time = df.index[i]

        equity_curve.append(capital)

    # 回测结束平仓
    if position != 0:
        price = float(df.iloc[-1]["close"])
        if position == 1:
            pnl = (price - entry_price) / entry_price * capital * lev
        else:
            pnl = (entry_price - price) / entry_price * capital * lev
        pnl -= capital * FEE_RATE * 2
        capital += pnl
        days = (df.index[-1] - entry_time).days if entry_time else 0
        trades.append(Trade(str(entry_time)[:10], str(df.index[-1])[:10], "多" if position == 1 else "空", entry_price, price, pnl, days))

    # 统计
    eq = pd.Series(equity_curve)
    total_ret = (capital - INITIAL) / INITIAL
    rets = eq.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0
    rolling_max = eq.cummax()
    dd = (eq - rolling_max) / rolling_max
    max_dd = float(dd.min())
    days_total = (df.index[-1] - df.index[0]).days
    cagr = (capital / INITIAL) ** (365 / max(days_total, 1)) - 1
    wins = sum(1 for t in trades if t.pnl > 0)
    win_rate = wins / len(trades) * 100 if trades else 0

    return {
        "final": capital,
        "total_ret": total_ret,
        "cagr": cagr,
        "sharpe": sharpe,
        "max_dd": max_dd,
        "trades": len(trades),
        "win_rate": win_rate,
        "trade_list": trades,
    }


def main():
    print("=" * 120)
    print("多市场 MA 趋势策略回测（日线，无杠杆，2019-2025）")
    print(f"  手续费: {FEE_RATE:.2%} | ATR 止损 3.0x | SMA200 趋势过滤 | high/low 盘中止损")
    print("=" * 120)

    # 拉数据
    data_cache = {}
    for name, ticker in ASSETS.items():
        df = fetch_data(ticker)
        if df is not None and len(df) > 300:
            data_cache[name] = df
            print(f"  {name:12} ({ticker:8}): {len(df)} 根日线 | {df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')}")
        else:
            print(f"  {name:12} ({ticker:8}): 数据不足，跳过")

    # 1. 先找每个市场的最优 MA 参数
    print(f"\n{'='*120}")
    print("各市场最优 MA 参数扫描")
    print(f"{'='*120}")
    header = f"{'资产':<14}"
    for p in MA_PARAMS:
        header += f"{'|':>2} {p['name']:>12}{'':>2}"
    print(header)
    print("-" * 80)

    best_params = {}  # asset_name -> best param name
    for name, df in data_cache.items():
        line = f"{name:<14}"
        best_sharpe = -999
        best_name = ""
        for p in MA_PARAMS:
            df_ma = add_ma(df, p["fast"], p["slow"])
            r = run_backtest(df_ma, p["fast"], p["slow"])
            line += f"| {r['total_ret']:>+7.1%} S{r['sharpe']:.1f}"
            if r["sharpe"] > best_sharpe:
                best_sharpe = r["sharpe"]
                best_name = p["name"]
        line += f"  ★{best_name}"
        print(line)
        best_params[name] = best_name

    # 2. 用 15/50 统一对比（公平比较）
    print(f"\n{'='*120}")
    print("统一 MA15/50 + SMA200 对比（日线，无杠杆）")
    print(f"{'='*120}")
    print(f"{'资产':<14}{'Return':>10}{'CAGR':>10}{'Sharpe':>10}{'MaxDD':>10}{'交易':>6}{'胜率':>6}{'最优MA':>10}")
    print("-" * 80)

    results_15_50 = {}
    for name, df in data_cache.items():
        df_ma = add_ma(df, 15, 50)
        r = run_backtest(df_ma, 15, 50)
        results_15_50[name] = r
        print(f"{name:<14}{r['total_ret']:>+10.1%}{r['cagr']:>+10.1%}{r['sharpe']:>10.2f}{r['max_dd']:>10.1%}{r['trades']:>6}{r['win_rate']:>5.0f}%{best_params[name]:>10}")

    # 3. 分类汇总
    print(f"\n{'='*120}")
    print("分类汇总（MA15/50）")
    print(f"{'='*120}")

    categories = {
        "商品期货": ["黄金", "原油", "铜", "白银", "天然气"],
        "美股指数": ["SPY(标普)", "QQQ(纳指)", "IWM(罗素)"],
        "加密币": ["BTC"],
    }

    for cat, names in categories.items():
        available = [n for n in names if n in results_15_50]
        if not available:
            continue
        avg_ret = np.mean([results_15_50[n]["total_ret"] for n in available])
        avg_sharpe = np.mean([results_15_50[n]["sharpe"] for n in available])
        avg_dd = np.mean([results_15_50[n]["max_dd"] for n in available])
        print(f"  {cat:<10}: 均值 Return {avg_ret:+.1%} | Sharpe {avg_sharpe:.2f} | MaxDD {avg_dd:.1%}")

    # 4. 模拟多资产组合
    print(f"\n{'='*120}")
    print("多资产组合模拟（等权分配，每个市场取最佳标的）")
    print(f"{'='*120}")

    # 选每个类别里 Sharpe 最高的
    portfolio_assets = {}
    for cat, names in categories.items():
        available = {n: results_15_50[n] for n in names if n in results_15_50}
        if available:
            best = max(available, key=lambda n: available[n]["sharpe"])
            portfolio_assets[f"{cat}:{best}"] = available[best]

    if len(portfolio_assets) > 1:
        avg_ret = np.mean([r["total_ret"] for r in portfolio_assets.values()])
        avg_sharpe = np.mean([r["sharpe"] for r in portfolio_assets.values()])
        avg_dd = np.mean([r["max_dd"] for r in portfolio_assets.values()])
        # 估算组合 DD（不相关资产组合的 DD ≈ 单资产 DD / sqrt(N)）
        n = len(portfolio_assets)
        est_dd = avg_dd / np.sqrt(n) * 1.5  # 保守估计，乘 1.5

        print(f"  组合成分:")
        for name, r in portfolio_assets.items():
            print(f"    {name}: Return {r['total_ret']:+.1%} | Sharpe {r['sharpe']:.2f} | DD {r['max_dd']:.1%}")
        print(f"\n  组合估算:")
        print(f"    均值 Return: {avg_ret:+.1%}")
        print(f"    均值 Sharpe: {avg_sharpe:.2f}")
        print(f"    估算 MaxDD:  {est_dd:.1%} (分散后)")
        print(f"\n  vs 单跑加密币:")
        if "BTC" in results_15_50:
            btc = results_15_50["BTC"]
            print(f"    BTC 单跑: Return {btc['total_ret']:+.1%} | Sharpe {btc['sharpe']:.2f} | DD {btc['max_dd']:.1%}")
            print(f"    组合优势: DD 改善 ~{(1 - est_dd/btc['max_dd'])*100:.0f}%，Sharpe 提升 {avg_sharpe - btc['sharpe']:+.2f}")


if __name__ == "__main__":
    main()
