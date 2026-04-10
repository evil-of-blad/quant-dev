"""
资金费率套利入口
用法: python funding_arb_runner.py
"""
import sys
import os
import asyncio
import signal
import yaml
from loguru import logger

sys.path.insert(0, os.path.dirname(__file__))

from arbitrage.funding_arb import FundingArbTrader


def load_config(path: str = "config/config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def setup_logger(config: dict):
    log_cfg = config.get("logging", {})
    logger.remove()
    logger.add(sys.stderr, level=log_cfg.get("level", "INFO"),
               format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")
    log_file = "logs/funding_arb.log"
    os.makedirs("logs", exist_ok=True)
    logger.add(log_file, rotation="10 MB", retention="30 days", level="DEBUG")


async def run(config: dict):
    trader = FundingArbTrader(config)

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

    # 检查配置
    arb_cfg = config.get("funding_arb", {})
    if not arb_cfg:
        logger.error("请在 config.yaml 中添加 funding_arb 配置")
        sys.exit(1)

    logger.info("[费率套利] 启动中...")
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
