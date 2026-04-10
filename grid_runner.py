"""
网格交易入口
用法: python grid_runner.py
"""
import sys
import os
import asyncio
import signal
import yaml
from loguru import logger

sys.path.insert(0, os.path.dirname(__file__))

from arbitrage.grid_trader import GridTrader


def load_config(path: str = "config/config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def setup_logger(config: dict):
    log_cfg = config.get("logging", {})
    logger.remove()
    logger.add(sys.stderr, level=log_cfg.get("level", "INFO"),
               format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")
    log_file = "logs/grid.log"
    os.makedirs("logs", exist_ok=True)
    logger.add(log_file, rotation="10 MB", retention="30 days", level="DEBUG")


async def run(config: dict):
    trader = GridTrader(config)

    def _shutdown(sig, frame):
        logger.info(f"收到信号 {sig}，正在关闭...")
        asyncio.create_task(trader.stop())

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        await trader.start()
    finally:
        await trader.stop()


def main():
    config = load_config()
    setup_logger(config)

    if not config.get("grid"):
        logger.error("请在 config.yaml 中添加 grid 配置")
        sys.exit(1)

    asyncio.run(run(config))


if __name__ == "__main__":
    main()
