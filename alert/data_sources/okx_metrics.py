"""
OKX 指标数据源 — 资金费率、K线（用于算 200 周均线偏离）
"""
import ccxt.async_support as ccxt
import pandas as pd
from loguru import logger


class OKXMetricsSource:

    def __init__(self):
        self.client = ccxt.okx({
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })

    async def close(self):
        try:
            await self.client.close()
        except Exception:
            pass

    async def fetch_funding_rate(self, symbol: str = "BTC/USDT:USDT") -> dict:
        """获取当前资金费率"""
        try:
            fr = await self.client.fetch_funding_rate(symbol)
            return {
                "symbol": symbol,
                "rate": fr.get("fundingRate", 0) or 0,
                "next_time": fr.get("fundingDatetime", ""),
            }
        except Exception as e:
            logger.warning(f"[OKX] 拉取费率失败: {e}")
            return None

    async def fetch_weekly_klines(self, symbol: str = "BTC/USDT:USDT", limit: int = 250) -> pd.DataFrame:
        """周线 K 线（用于算 200 周均线）"""
        try:
            data = await self.client.fetch_ohlcv(symbol, "1w", limit=limit)
            df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("timestamp", inplace=True)
            return df
        except Exception as e:
            logger.warning(f"[OKX] 拉取周线失败: {e}")
            return None

    async def fetch_ma200_deviation(self, symbol: str = "BTC/USDT:USDT") -> dict:
        """
        200 周均线偏离度
        - 牛市顶部时通常偏离 +200% 以上
        - 熊市底部时通常偏离 -50% 以下
        """
        df = await self.fetch_weekly_klines(symbol, limit=210)
        if df is None or len(df) < 200:
            return None

        ma200 = df["close"].rolling(200).mean().iloc[-1]
        curr = df["close"].iloc[-1]
        deviation_pct = (curr - ma200) / ma200 * 100

        return {
            "symbol": symbol,
            "current": curr,
            "ma200w": ma200,
            "deviation_pct": deviation_pct,
        }

    async def fetch_klines_history(self, symbol: str, timeframe: str, since: int, limit: int = 1000) -> pd.DataFrame:
        """获取历史 K 线（用于回测）"""
        try:
            data = await self.client.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
            df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("timestamp", inplace=True)
            return df
        except Exception as e:
            logger.warning(f"[OKX] 拉取历史K线失败: {e}")
            return None
