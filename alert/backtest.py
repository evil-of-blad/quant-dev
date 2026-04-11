"""
告警系统历史回溯
- 拉取 5 年历史指标数据
- 模拟告警引擎在每个时间点的判断
- 标记历史已知的真实大底大顶事件
- 输出图表 + 准确率报告
"""
import asyncio
import os
import aiohttp
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta
from loguru import logger


async def fetch_okx_history_candles(inst_id: str, days: int) -> pd.DataFrame:
    """
    使用 OKX history-candles 接口（无需 API Key），分页拉取历史数据
    支持非常长的历史
    """
    BASE = "https://www.okx.com/api/v5/market/history-candles"
    all_data = []
    after = ""  # 空表示从最新开始

    target_count = days
    async with aiohttp.ClientSession() as session:
        while len(all_data) < target_count:
            params = {"instId": inst_id, "bar": "1Dutc", "limit": "100"}
            if after:
                params["after"] = after
            try:
                async with session.get(BASE, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    data = await resp.json()
                    if data.get("code") != "0":
                        logger.warning(f"[OKX] 请求失败: {data}")
                        break
                    items = data.get("data", [])
                    if not items:
                        break
                    all_data.extend(items)
                    after = items[-1][0]  # 最老的 ts
                    await asyncio.sleep(0.1)
            except Exception as e:
                logger.warning(f"[OKX] 拉取异常: {e}")
                break

    if not all_data:
        return None

    df = pd.DataFrame(all_data, columns=["ts", "open", "high", "low", "close", "volume", "v2", "v3", "v4"])
    df["date"] = pd.to_datetime(df["ts"].astype(int), unit="ms").dt.normalize()
    df = df[["date", "close"]].copy()
    df["close"] = df["close"].astype(float)
    df.set_index("date", inplace=True)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df

from .data_sources.fear_greed import FearGreedSource
from .data_sources.coingecko import CoinGeckoSource
from .data_sources.okx_metrics import OKXMetricsSource
from . import indicators


# 已知历史大底/大顶事件
KNOWN_EVENTS = [
    ("2018-12-15", "bottom", "2018 熊市底部", 3300),
    ("2020-03-12", "bottom", "312 闪崩底", 4800),
    ("2020-12-16", "breakout", "突破 ATH", 23000),
    ("2021-04-14", "top", "牛市第一顶", 64800),
    ("2021-07-20", "bottom", "牛市中期底", 29800),
    ("2021-11-10", "top", "牛市最终顶", 69000),
    ("2022-06-18", "bottom", "Luna 崩盘后底", 17700),
    ("2022-11-21", "bottom", "FTX 崩盘底", 15500),
    ("2024-03-13", "top", "ETF 牛市顶", 73800),
    ("2024-08-05", "bottom", "8月暴跌底", 49000),
    ("2025-01-20", "top", "1月顶", 109000),
]


async def run_backtest(years_back: int = 5, output_dir: str = "logs/alert_backtest"):
    os.makedirs(output_dir, exist_ok=True)

    logger.info(f"[回溯] 开始拉取 {years_back} 年数据...")

    # 需要多拉一些数据用于 200 周均线（200周=约4年）
    fetch_days = (years_back + 4) * 365

    # 1. 用 OKX history-candles 接口（现货，支持长历史）
    btc_df = await fetch_okx_history_candles("BTC-USDT", fetch_days)
    if btc_df is None or btc_df.empty:
        logger.error("[回溯] BTC 历史拉取失败")
        return
    logger.info(f"[回溯] BTC 日线: {len(btc_df)} 根 ({btc_df.index[0]} ~ {btc_df.index[-1]})")

    # 200 日均线偏离（牛市顶通常 +50%~+100%，熊市底 -30%~-50%）
    btc_df["ma200d"] = btc_df["close"].rolling(200).mean()
    btc_df["ma200_dev_pct"] = (btc_df["close"] - btc_df["ma200d"]) / btc_df["ma200d"] * 100

    # 2. 拉取恐贪指数历史
    fg_src = FearGreedSource()
    fg_history = await fg_src.fetch_history(days=years_back * 365)
    fg_df = pd.DataFrame(fg_history)
    if not fg_df.empty:
        fg_df["date"] = pd.to_datetime(fg_df["date"])
        fg_df.set_index("date", inplace=True)
        fg_df = fg_df.sort_index()
        logger.info(f"[回溯] 恐贪指数: {len(fg_df)} 条")
    else:
        logger.warning("[回溯] 恐贪指数拉取失败")
        return

    # 3. 合并数据
    btc_df.index = btc_df.index.normalize()
    merged = btc_df.join(fg_df[["value"]].rename(columns={"value": "fear_greed"}), how="left")
    merged["fear_greed"] = merged["fear_greed"].ffill()

    # 4. 计算每日的"评分"（用核心指标的简化版）
    scores_list = []
    for idx, row in merged.iterrows():
        if pd.isna(row["fear_greed"]) or pd.isna(row["ma200_dev_pct"]):
            scores_list.append({"date": idx, "score": 0, "direction": "neutral"})
            continue

        fg_score = indicators.score_fear_greed(row["fear_greed"])
        ma_score = indicators.score_ma200_deviation(row["ma200_dev_pct"])

        # 简化加权
        bullish = 0
        bearish = 0
        weight = 0
        for s in [(fg_score, 1.5), (ma_score, 1.5)]:
            sc, w = s
            weight += w
            if "bullish" in sc.direction:
                bullish += sc.score * w
            elif "bearish" in sc.direction:
                bearish += sc.score * w

        if weight > 0:
            bullish_avg = bullish / weight
            bearish_avg = bearish / weight
            if bullish_avg > bearish_avg:
                score = bullish_avg
                direction = "bullish"
            else:
                score = bearish_avg
                direction = "bearish"
        else:
            score = 0
            direction = "neutral"

        scores_list.append({"date": idx, "score": score, "direction": direction,
                            "fg": row["fear_greed"], "ma_dev": row["ma200_dev_pct"]})

    scores_df = pd.DataFrame(scores_list).set_index("date")
    merged = merged.join(scores_df[["score", "direction"]])

    # 5. 找出告警触发点
    alerts = []
    for idx, row in merged.iterrows():
        if pd.isna(row["score"]):
            continue
        if row["score"] >= 50:
            level = "WARNING" if row["score"] < 70 else ("CRITICAL" if row["score"] < 85 else "EMERGENCY")
            alerts.append({
                "date": idx,
                "score": row["score"],
                "direction": row["direction"],
                "level": level,
                "btc_price": row["close"],
            })

    alerts_df = pd.DataFrame(alerts)
    logger.info(f"[回溯] 总告警次数: {len(alerts_df)}")

    # 6. 验证准确率（事件前 60 天 ~ 后 5 天为有效预警窗口）
    accuracy_results = []
    for ev_date, ev_type, ev_name, _ in KNOWN_EVENTS:
        ev_dt = pd.Timestamp(ev_date)
        if alerts_df.empty:
            window = alerts_df
        else:
            window = alerts_df[
                (alerts_df["date"] >= ev_dt - timedelta(days=60)) &
                (alerts_df["date"] <= ev_dt + timedelta(days=5))
            ]
        if ev_type == "bottom":
            match = window[window["direction"] == "bullish"]
        elif ev_type == "top":
            match = window[window["direction"] == "bearish"]
        else:
            match = window

        hit = len(match) > 0
        first_alert_date = match["date"].min() if hit else None
        days_ahead = (ev_dt - first_alert_date).days if first_alert_date else None

        accuracy_results.append({
            "event_date": ev_date,
            "event_name": ev_name,
            "event_type": ev_type,
            "hit": hit,
            "days_ahead": days_ahead,
        })

    acc_df = pd.DataFrame(accuracy_results)
    hit_rate = acc_df["hit"].mean() * 100
    avg_days = acc_df[acc_df["days_ahead"].notna()]["days_ahead"].mean() if len(acc_df[acc_df["days_ahead"].notna()]) > 0 else 0

    logger.info(f"[回溯] 准确率: {hit_rate:.0f}% ({acc_df['hit'].sum()}/{len(acc_df)})")
    logger.info(f"[回溯] 平均提前天数: {avg_days:.1f}")

    # 7. 绘图
    plot_path = os.path.join(output_dir, "alert_backtest.png")
    plot_results(merged, alerts_df, acc_df, plot_path)
    logger.info(f"[回溯] 图表已保存: {plot_path}")

    # 8. Markdown 报告
    md_path = os.path.join(output_dir, "report.md")
    write_markdown_report(merged, alerts_df, acc_df, hit_rate, avg_days, md_path)
    logger.info(f"[回溯] 报告已保存: {md_path}")

    return {
        "alerts": len(alerts_df),
        "events": len(acc_df),
        "hit_rate": hit_rate,
        "avg_days_ahead": avg_days,
    }


def plot_results(merged: pd.DataFrame, alerts_df: pd.DataFrame, acc_df: pd.DataFrame, save_path: str):
    """绘制 BTC 价格 + 告警标记 + 历史事件"""
    plt.rcParams["font.family"] = ["Arial Unicode MS", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, axes = plt.subplots(3, 1, figsize=(16, 12), sharex=True)

    # ============ 1. BTC 价格 + 告警标记 + 事件 ============
    ax1 = axes[0]
    ax1.plot(merged.index, merged["close"], color="#2196F3", linewidth=1, label="BTC")
    ax1.set_ylabel("BTC Price (USD)")
    ax1.set_yscale("log")
    ax1.set_title("BTC 价格 + 告警标记 + 历史事件")
    ax1.grid(True, alpha=0.3)

    # 标记告警
    if not alerts_df.empty:
        bull = alerts_df[alerts_df["direction"] == "bullish"]
        bear = alerts_df[alerts_df["direction"] == "bearish"]
        if not bull.empty:
            ax1.scatter(bull["date"], bull["btc_price"], marker="^", color="lime",
                        s=30, alpha=0.6, label=f"看多告警 ({len(bull)})", zorder=5)
        if not bear.empty:
            ax1.scatter(bear["date"], bear["btc_price"], marker="v", color="red",
                        s=30, alpha=0.6, label=f"看空告警 ({len(bear)})", zorder=5)

    # 标记历史事件
    for ev_date, ev_type, ev_name, ev_price in KNOWN_EVENTS:
        ev_dt = pd.Timestamp(ev_date)
        if ev_dt < merged.index[0] or ev_dt > merged.index[-1]:
            continue
        color = "green" if ev_type == "bottom" else ("red" if ev_type == "top" else "orange")
        ax1.axvline(ev_dt, color=color, linestyle="--", alpha=0.5, linewidth=1)
        ax1.annotate(ev_name, xy=(ev_dt, ev_price),
                     xytext=(5, 10), textcoords="offset points",
                     fontsize=7, color=color, rotation=45)

    ax1.legend(loc="upper left", fontsize=8)

    # ============ 2. 综合评分 ============
    ax2 = axes[1]
    # 用颜色编码 direction
    bull_data = merged[merged["direction"] == "bullish"]
    bear_data = merged[merged["direction"] == "bearish"]
    if not bull_data.empty:
        ax2.fill_between(bull_data.index, 0, bull_data["score"],
                         color="green", alpha=0.5, label="看多评分")
    if not bear_data.empty:
        ax2.fill_between(bear_data.index, 0, bear_data["score"],
                         color="red", alpha=0.5, label="看空评分")
    ax2.axhline(50, color="orange", linestyle="--", alpha=0.5, label="WARNING (50)")
    ax2.axhline(70, color="red", linestyle="--", alpha=0.5, label="CRITICAL (70)")
    ax2.set_ylabel("Score")
    ax2.set_title("告警综合评分")
    ax2.set_ylim(0, 100)
    ax2.legend(loc="upper left", fontsize=8)
    ax2.grid(True, alpha=0.3)

    # ============ 3. 恐贪指数 ============
    ax3 = axes[2]
    ax3.plot(merged.index, merged["fear_greed"], color="#9C27B0", linewidth=1)
    ax3.fill_between(merged.index, 0, merged["fear_greed"],
                     where=(merged["fear_greed"] < 25), color="green", alpha=0.3, label="极度恐惧")
    ax3.fill_between(merged.index, merged["fear_greed"], 100,
                     where=(merged["fear_greed"] > 75), color="red", alpha=0.3, label="极度贪婪")
    ax3.axhline(25, color="green", linestyle=":", alpha=0.5)
    ax3.axhline(75, color="red", linestyle=":", alpha=0.5)
    ax3.set_ylabel("Fear & Greed Index")
    ax3.set_title("恐贪指数")
    ax3.set_ylim(0, 100)
    ax3.legend(loc="upper left", fontsize=8)
    ax3.grid(True, alpha=0.3)

    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    plt.setp(ax3.xaxis.get_majorticklabels(), rotation=45)

    plt.tight_layout()
    plt.savefig(save_path, dpi=120, bbox_inches="tight")
    plt.close()


def write_markdown_report(merged, alerts_df, acc_df, hit_rate, avg_days, save_path):
    """生成 markdown 报告"""
    lines = [
        "# 告警系统历史回溯报告",
        "",
        f"**回溯区间:** {merged.index[0].strftime('%Y-%m-%d')} ~ {merged.index[-1].strftime('%Y-%m-%d')}",
        f"**总数据天数:** {len(merged)}",
        f"**总告警次数:** {len(alerts_df)}",
        "",
        "## 准确率",
        "",
        f"- **历史事件数:** {len(acc_df)}",
        f"- **命中数:** {acc_df['hit'].sum()}",
        f"- **准确率:** {hit_rate:.0f}%",
        f"- **平均提前天数:** {avg_days:.1f}",
        "",
        "## 历史事件验证",
        "",
        "| 日期 | 事件 | 类型 | 命中 | 提前天数 |",
        "|------|------|------|------|---------|",
    ]
    for _, row in acc_df.iterrows():
        hit_str = "✅" if row["hit"] else "❌"
        days = f"{int(row['days_ahead'])}" if pd.notna(row["days_ahead"]) else "-"
        lines.append(f"| {row['event_date']} | {row['event_name']} | {row['event_type']} | {hit_str} | {days} |")

    lines.append("")
    lines.append("## 告警分布")
    lines.append("")
    if not alerts_df.empty:
        bull_count = (alerts_df["direction"] == "bullish").sum()
        bear_count = (alerts_df["direction"] == "bearish").sum()
        lines.append(f"- **看多告警:** {bull_count}")
        lines.append(f"- **看空告警:** {bear_count}")
        lines.append("")
        lines.append("### 等级分布")
        for level in ["WARNING", "CRITICAL", "EMERGENCY"]:
            cnt = (alerts_df["level"] == level).sum()
            lines.append(f"- {level}: {cnt}")

    with open(save_path, "w") as f:
        f.write("\n".join(lines))
