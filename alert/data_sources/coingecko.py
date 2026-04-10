"""
CoinGecko 数据源 — 市值/dominance/价格
免费版限制 30 次/分钟
"""
import aiohttp
from loguru import logger


class CoinGeckoSource:
    BASE = "https://api.coingecko.com/api/v3"

    async def fetch_global(self) -> dict:
        """
        全局市场数据，包含 BTC dominance
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.BASE}/global", timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    data = await resp.json()
                    d = data.get("data", {})
                    return {
                        "total_mcap": d.get("total_market_cap", {}).get("usd", 0),
                        "btc_dominance": d.get("market_cap_percentage", {}).get("btc", 0),
                        "eth_dominance": d.get("market_cap_percentage", {}).get("eth", 0),
                        "active_cryptocurrencies": d.get("active_cryptocurrencies", 0),
                        "mcap_change_24h_pct": d.get("market_cap_change_percentage_24h_usd", 0),
                    }
        except Exception as e:
            logger.warning(f"[CoinGecko] 拉取全局数据失败: {e}")
            return None

    async def fetch_btc_history(self, days: int = 365) -> list[dict]:
        """BTC 价格历史"""
        try:
            async with aiohttp.ClientSession() as session:
                params = {"vs_currency": "usd", "days": days, "interval": "daily"}
                async with session.get(
                    f"{self.BASE}/coins/bitcoin/market_chart",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    data = await resp.json()
                    prices = data.get("prices", [])
                    return [{"timestamp": p[0], "price": p[1]} for p in prices]
        except Exception as e:
            logger.warning(f"[CoinGecko] 拉取BTC历史失败: {e}")
            return []
