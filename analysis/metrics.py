"""
绩效指标计算
"""
import pandas as pd
import numpy as np
from rich.console import Console
from rich.table import Table


def compute_metrics(result: dict) -> dict:
    """从回测结果中提取所有绩效指标"""
    equity_df: pd.DataFrame = result.get("equity_curve", pd.DataFrame())

    if equity_df.empty:
        return result

    equity = equity_df["equity"]
    returns = equity.pct_change().dropna()

    # 年化因子（按小时 K 线 ≈ 24*365 根）
    total_bars = len(equity)
    ann_factor = 365 * 24 / total_bars  # 粗估，实际按 timeframe 调整

    sharpe = (returns.mean() / returns.std() * np.sqrt(365 * 24)) if returns.std() > 0 else 0
    rolling_max = equity.cummax()
    drawdown_series = (equity - rolling_max) / rolling_max
    max_drawdown = drawdown_series.min()
    calmar = result["total_return"] / abs(max_drawdown) if max_drawdown != 0 else 0

    trades = pd.DataFrame(result.get("trades", []))
    win_rate = result.get("win_rate", 0)
    avg_win = trades[trades["pnl"] > 0]["pnl"].mean() if not trades.empty else 0
    avg_loss = trades[trades["pnl"] < 0]["pnl"].mean() if not trades.empty else 0
    profit_factor = (
        abs(trades[trades["pnl"] > 0]["pnl"].sum() / trades[trades["pnl"] < 0]["pnl"].sum())
        if not trades.empty and trades[trades["pnl"] < 0]["pnl"].sum() != 0
        else 0
    )

    return {
        **result,
        "sharpe_ratio": sharpe,
        "max_drawdown": max_drawdown,
        "calmar_ratio": calmar,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "drawdown_series": drawdown_series,
    }


def print_report(result: dict, strategy_name: str = ""):
    """用 Rich 在终端打印回测报告"""
    console = Console()

    console.print(f"\n[bold cyan]{'='*50}[/bold cyan]")
    console.print(f"[bold cyan]  回测报告 — {strategy_name or result.get('symbol', '')}[/bold cyan]")
    console.print(f"[bold cyan]{'='*50}[/bold cyan]\n")

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("指标", style="dim", width=20)
    table.add_column("数值", justify="right")

    def fmt_pct(v):
        color = "green" if v >= 0 else "red"
        return f"[{color}]{v:.2%}[/{color}]"

    def fmt_num(v, decimals=4):
        color = "green" if v >= 0 else "red"
        return f"[{color}]{v:.{decimals}f}[/{color}]"

    table.add_row("初始资金", f"${result['initial_capital']:,.2f}")
    table.add_row("最终权益", f"${result['final_equity']:,.2f}")
    table.add_row("总收益率", fmt_pct(result["total_return"]))
    table.add_row("夏普比率", fmt_num(result.get("sharpe_ratio", 0), 3))
    table.add_row("最大回撤", fmt_pct(result.get("max_drawdown", 0)))
    table.add_row("卡玛比率", fmt_num(result.get("calmar_ratio", 0), 3))
    table.add_row("盈利因子", fmt_num(result.get("profit_factor", 0), 3))
    table.add_row("总交易次数", str(result.get("total_trades", 0)))
    table.add_row("胜率", fmt_pct(result.get("win_rate", 0)))
    table.add_row("平均盈利", fmt_num(result.get("avg_win", 0), 4))
    table.add_row("平均亏损", fmt_num(result.get("avg_loss", 0), 4))
    table.add_row("总手续费", f"${result.get('total_fee', 0):,.4f}")

    console.print(table)
    console.print()
