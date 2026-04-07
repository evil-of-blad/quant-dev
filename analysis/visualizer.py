"""
可视化模块 — 权益曲线、回撤、交易标注
"""
import os
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from loguru import logger


plt.rcParams["font.family"] = ["Arial Unicode MS", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def plot_backtest(result: dict, strategy_name: str = "", save_path: str = ""):
    """
    绘制四联图：
    1. 权益曲线
    2. 回撤
    3. 每笔交易 PnL 分布
    4. K 线收益散点（时间 vs PnL）
    """
    equity_df: pd.DataFrame = result.get("equity_curve", pd.DataFrame())
    if equity_df.empty:
        logger.warning("没有权益曲线数据，跳过绘图")
        return

    trades = pd.DataFrame(result.get("trades", []))
    drawdown = result.get("drawdown_series", pd.Series(dtype=float))

    fig, axes = plt.subplots(2, 2, figsize=(16, 10))
    fig.suptitle(
        f"回测报告 — {strategy_name} | {result.get('symbol', '')} "
        f"| 收益: {result['total_return']:.2%} "
        f"| Sharpe: {result.get('sharpe_ratio', 0):.2f} "
        f"| 最大回撤: {result.get('max_drawdown', 0):.2%}",
        fontsize=13,
    )

    # --- 1. 权益曲线 ---
    ax1 = axes[0, 0]
    ax1.plot(equity_df.index, equity_df["equity"], color="#2196F3", linewidth=1.5, label="权益")
    ax1.axhline(result["initial_capital"], color="gray", linestyle="--", linewidth=0.8, label="初始资金")
    if not trades.empty and "timestamp" in trades.columns:
        buys = trades[trades["side"] == "buy"]
        sells = trades[trades["side"] == "sell"]
        # 尝试标注买卖点
        for _, t in buys.iterrows():
            ts = t["timestamp"]
            idx = equity_df.index.get_indexer([ts], method="nearest")[0]
            if 0 <= idx < len(equity_df):
                ax1.scatter(equity_df.index[idx], equity_df["equity"].iloc[idx], marker="^", color="lime", s=40, zorder=5)
        for _, t in sells.iterrows():
            ts = t["timestamp"]
            idx = equity_df.index.get_indexer([ts], method="nearest")[0]
            if 0 <= idx < len(equity_df):
                ax1.scatter(equity_df.index[idx], equity_df["equity"].iloc[idx], marker="v", color="red", s=40, zorder=5)
    ax1.set_title("权益曲线")
    ax1.set_ylabel("USDT")
    ax1.legend(fontsize=8)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax1.tick_params(axis="x", rotation=30)

    # --- 2. 回撤 ---
    ax2 = axes[0, 1]
    if not drawdown.empty:
        ax2.fill_between(drawdown.index, drawdown * 100, 0, color="#F44336", alpha=0.6)
        ax2.plot(drawdown.index, drawdown * 100, color="#F44336", linewidth=0.8)
    ax2.set_title("回撤 (%)")
    ax2.set_ylabel("%")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax2.tick_params(axis="x", rotation=30)

    # --- 3. 每笔 PnL 分布 ---
    ax3 = axes[1, 0]
    if not trades.empty and "pnl" in trades.columns:
        pnl = trades["pnl"]
        colors = ["#4CAF50" if p >= 0 else "#F44336" for p in pnl]
        ax3.bar(range(len(pnl)), pnl, color=colors, width=0.7)
        ax3.axhline(0, color="black", linewidth=0.8)
    ax3.set_title("每笔交易 PnL")
    ax3.set_xlabel("交易序号")
    ax3.set_ylabel("USDT")

    # --- 4. 累计盈亏 ---
    ax4 = axes[1, 1]
    if not trades.empty and "pnl" in trades.columns:
        cumulative = trades["pnl"].cumsum()
        ax4.plot(cumulative.values, color="#9C27B0", linewidth=1.5)
        ax4.fill_between(range(len(cumulative)), cumulative.values, 0, alpha=0.2, color="#9C27B0")
        ax4.axhline(0, color="black", linewidth=0.8)
    ax4.set_title("累计 PnL")
    ax4.set_xlabel("交易序号")
    ax4.set_ylabel("USDT")

    plt.tight_layout()

    if save_path:
        os.makedirs(os.path.dirname(save_path) if os.path.dirname(save_path) else ".", exist_ok=True)
        plt.savefig(save_path, dpi=150, bbox_inches="tight")
        logger.info(f"图表已保存: {save_path}")
    else:
        plt.show()

    plt.close()
