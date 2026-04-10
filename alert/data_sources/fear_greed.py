"""
恐贪指数数据源 — alternative.me
免费 API，无需 key
"""
import aiohttp
from loguru import logger
from datetime import datetime


class FearGreedSource:
    URL = "https://api.alternative.me/fng/"

    def __init__(self):
        self._cache: dict = None
        self._cache_time: datetime = None

    async def fetch(self) -> dict:
        """
        返回:
        {
            "value": 25,           # 0-100
            "classification": "Fear",
            "timestamp": "2026-04-11 12:00:00"
        }
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.URL, params={"limit": 1}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    item = data.get("data", [{}])[0]
                    return {
                        "value": int(item.get("value", 50)),
                        "classification": item.get("value_classification", "Neutral"),
                        "timestamp": datetime.fromtimestamp(int(item.get("timestamp", 0))).strftime("%Y-%m-%d %H:%M:%S"),
                    }
        except Exception as e:
            logger.warning(f"[FearGreed] 拉取失败: {e}")
            return None

    async def fetch_history(self, days: int = 365) -> list[dict]:
        """拉取历史数据用于回溯"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.URL, params={"limit": days}, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    data = await resp.json()
                    return [
                        {
                            "value": int(item["value"]),
                            "classification": item["value_classification"],
                            "date": datetime.fromtimestamp(int(item["timestamp"])).strftime("%Y-%m-%d"),
                        }
                        for item in data.get("data", [])
                    ]
        except Exception as e:
            logger.warning(f"[FearGreed] 拉取历史失败: {e}")
            return []
