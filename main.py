"""
实盘交易入口
用法:
    python main.py
    python main.py --strategy rsi --symbol BTC/USDT
"""
import sys
import os
import asyncio
import argparse
import signal
import yaml
from loguru import logger

sys.path.insert(0, os.path.dirname(__file__))

from core.exchange import AsyncExchange
from strategies.registry import get_strategy
from live.trader import LiveTrader


def load_config(path: str = "config/config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def setup_logger(config: dict):
    log_cfg = config.get("logging", {})
    logger.remove()
    logger.add(sys.stderr, level=log_cfg.get("level", "INFO"),
               format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")
    log_file = log_cfg.get("file", "logs/quant.log")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    logger.add(log_file, rotation="10 MB", retention="30 days", level="DEBUG")


async def run_live(config: dict, strategy_name: str):
    strategy_params = config["strategy"].get("params", {})
    strategy = get_strategy(strategy_name, strategy_params)
    trader = LiveTrader(config, strategy)

    loop = asyncio.get_event_loop()

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
    parser = argparse.ArgumentParser(description="量化实盘交易系统")
    parser.add_argument("--config", default="config/config.yaml", help="配置文件路径")
    parser.add_argument("--strategy", default=None, help="策略名 (ma_crossover/rsi/bollinger_bands)")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logger(config)

    # 检查 API Key
    ex_cfg = config["exchange"]
    if not ex_cfg.get("api_key") or not ex_cfg.get("api_secret") or not ex_cfg.get("passphrase"):
        logger.error("请在 config/config.yaml 中配置 exchange.api_key / api_secret / passphrase！")
        sys.exit(1)

    strategy_name = args.strategy or config["strategy"]["name"]
    sandbox = config["exchange"].get("sandbox", True)
    logger.info(f"{'[测试网]' if sandbox else '[正式网络]'} 策略: {strategy_name}")

    asyncio.run(run_live(config, strategy_name))


if __name__ == "__main__":
    main()
