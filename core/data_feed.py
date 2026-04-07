"""
数据获取与缓存模块
"""
import os
import pandas as pd
from loguru import logger
from .exchange import Exchange


DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


def _cache_path(symbol: str, timeframe: str, start: str, end: str) -> str:
    safe_sym = symbol.replace("/", "_")
    return os.path.join(DATA_DIR, f"{safe_sym}_{timeframe}_{start}_{end}.parquet")


def get_historical_data(
    exchange: Exchange,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    拉取历史 K 线，优先从本地缓存读取。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    cache = _cache_path(symbol, timeframe, start, end)

    if use_cache and os.path.exists(cache):
        logger.info(f"从缓存加载: {cache}")
        return pd.read_parquet(cache)

    df = exchange.fetch_ohlcv_range(symbol, timeframe, start, end)
    df.to_parquet(cache)
    logger.info(f"数据已缓存: {cache}")
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """给 OHLCV 数据添加常用技术指标（供策略使用）"""
    import ta

    df = df.copy()
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    # 趋势指标
    df["ema_9"] = ta.trend.ema_indicator(close, window=9)
    df["ema_21"] = ta.trend.ema_indicator(close, window=21)
    df["sma_10"] = ta.trend.sma_indicator(close, window=10)
    df["sma_30"] = ta.trend.sma_indicator(close, window=30)
    df["sma_50"] = ta.trend.sma_indicator(close, window=50)
    df["sma_200"] = ta.trend.sma_indicator(close, window=200)

    # MACD
    macd = ta.trend.MACD(close)
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    # RSI
    df["rsi_14"] = ta.momentum.RSIIndicator(close, window=14).rsi()

    # Bollinger Bands
    bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_mid"] = bb.bollinger_mavg()
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_width"] = bb.bollinger_wband()

    # ATR (平均真实波幅)
    df["atr_14"] = ta.volatility.AverageTrueRange(high, low, close, window=14).average_true_range()

    # 成交量
    df["vol_sma_20"] = ta.trend.sma_indicator(volume, window=20)

    # ADX（平均趋向指数，判断趋势强弱）
    adx = ta.trend.ADXIndicator(high, low, close, window=14)
    df["adx_14"] = adx.adx()
    df["adx_pos"] = adx.adx_pos()   # +DI
    df["adx_neg"] = adx.adx_neg()   # -DI

    df.dropna(inplace=True)
    return df
