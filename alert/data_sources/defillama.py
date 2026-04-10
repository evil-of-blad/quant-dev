"""
DefiLlama 数据源 — 稳定币市值
免费 API
"""
import aiohttp
from loguru import logger


class DefiLlamaSource:
    BASE = "https://api.llama.fi"
    STABLE_BASE = "https://stablecoins.llama.fi"

    async def fetch_stablecoins(self) -> dict:
        """
        所有稳定币的总市值
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.STABLE_BASE}/stablecoincharts/all",
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    data = await resp.json()
                    if not data:
                        return None

                    latest = data[-1]
                    week_ago = data[-7] if len(data) >= 7 else data[0]

                    latest_mcap = sum(latest.get("totalCirculatingUSD", {}).values())
                    week_mcap = sum(week_ago.get("totalCirculatingUSD", {}).values())
                    week_change_pct = (latest_mcap - week_mcap) / week_mcap * 100 if week_mcap > 0 else 0

                    return {
                        "total_mcap": latest_mcap,
                        "week_change_pct": week_change_pct,
                        "history": [
                            {
                                "date": d.get("date"),
                                "mcap": sum(d.get("totalCirculatingUSD", {}).values()),
                            }
                            for d in data[-90:]  # 最近 90 天
                        ],
                    }
        except Exception as e:
            logger.warning(f"[DefiLlama] 拉取稳定币失败: {e}")
            return None
