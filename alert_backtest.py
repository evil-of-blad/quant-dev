"""
告警系统历史回溯入口
用法: python alert_backtest.py
"""
import sys
import os
import asyncio
from loguru import logger

sys.path.insert(0, os.path.dirname(__file__))

from alert.backtest import run_backtest


async def main():
    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")

    result = await run_backtest(years_back=5)
    if result:
        print()
        print("=" * 50)
        print(f"  告警次数: {result['alerts']}")
        print(f"  事件数:   {result['events']}")
        print(f"  准确率:   {result['hit_rate']:.0f}%")
        print(f"  平均提前: {result['avg_days_ahead']:.1f} 天")
        print("=" * 50)
        print()
        print("详细报告: logs/alert_backtest/report.md")
        print("图表:     logs/alert_backtest/alert_backtest.png")


if __name__ == "__main__":
    asyncio.run(main())
