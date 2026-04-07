"""
回测入口脚本
用法:
    python backtest_runner.py
    python backtest_runner.py --strategy rsi --symbol ETH/USDT
    python backtest_runner.py --strategy bollinger_bands --start 2024-01-01 --end 2024-06-30
"""
import sys
import os
import argparse
import yaml
from loguru import logger

sys.path.insert(0, os.path.dirname(__file__))

from core.exchange import Exchange
from core.data_feed import get_historical_data, add_indicators
from core.backtester import Backtester
from strategies.registry import get_strategy
from analysis.metrics import compute_metrics, print_report
from analysis.visualizer import plot_backtest


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
    logger.add(log_file, rotation="10 MB", retention="7 days", level="DEBUG")


def run_backtest(config: dict, strategy_name: str, symbol: str, start: str, end: str, plot: bool = True):
    # 初始化交易所（只用于拉数据，无需真实 key）
    exchange = Exchange(config)

    # 拉取历史数据
    df = get_historical_data(
        exchange,
        symbol,
        config["trading"]["timeframe"],
        start,
        end,
        use_cache=True,
    )

    # 添加技术指标
    df = add_indicators(df)

    # 初始化策略
    strategy_params = config["strategy"].get("params", {})
    strategy = get_strategy(strategy_name, strategy_params)

    # 回测
    backtester = Backtester(config)
    result = backtester.run(df, strategy, symbol)

    # 计算指标
    result = compute_metrics(result)

    # 打印报告
    print_report(result, strategy_name=f"{strategy_name} | {symbol}")

    # 绘图
    if plot:
        save_path = f"logs/backtest_{strategy_name}_{symbol.replace('/', '_')}_{start}_{end}.png"
        plot_backtest(result, strategy_name=strategy_name, save_path=save_path)

    return result


def main():
    parser = argparse.ArgumentParser(description="量化回测系统")
    parser.add_argument("--config", default="config/config.yaml", help="配置文件路径")
    parser.add_argument("--strategy", default=None, help="策略名 (ma_crossover/rsi/bollinger_bands)")
    parser.add_argument("--symbol", default=None, help="交易对，如 BTC/USDT")
    parser.add_argument("--start", default=None, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--no-plot", action="store_true", help="不生成图表")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logger(config)

    strategy_name = args.strategy or config["strategy"]["name"]
    symbol = args.symbol or config["trading"]["symbols"][0]
    start = args.start or config["backtest"]["start_date"]
    end = args.end or config["backtest"]["end_date"]

    logger.info(f"策略: {strategy_name} | 标的: {symbol} | 区间: {start} → {end}")
    run_backtest(config, strategy_name, symbol, start, end, plot=not args.no_plot)


if __name__ == "__main__":
    main()
