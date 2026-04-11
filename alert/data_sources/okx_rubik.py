"""
OKX Rubik 公开数据 — 持仓量、大户多空比、爆仓数据
全部免费，无需 API Key
"""
import aiohttp
import pandas as pd
from datetime import datetime
from loguru import logger


class OKXRubikSource:
    BASE = "https://www.okx.com/api/v5"

    async def fetch_open_interest(self, inst_id: str = "BTC-USDT-SWAP") -> dict:
        """当前持仓量"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.BASE}/public/open-interest",
                    params={"instType": "SWAP", "instId": inst_id},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("code") != "0":
                        return None
                    item = data["data"][0]
                    return {
                        "inst_id": inst_id,
                        "oi_contracts": float(item["oi"]),
                        "oi_btc": float(item["oiCcy"]),
                        "oi_usd": float(item["oiUsd"]),
                        "timestamp": int(item["ts"]),
                    }
        except Exception as e:
            logger.warning(f"[OKXRubik] OI 拉取失败: {e}")
            return None

    async def fetch_long_short_ratio(self, ccy: str = "BTC", period: str = "1D") -> dict:
        """
        大户多空账户比 (Top trader long-short ratio)
        period: 5m / 1H / 4H / 1D
        值 > 1 多头主导，< 1 空头主导
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.BASE}/rubik/stat/contracts/long-short-account-ratio",
                    params={"ccy": ccy, "period": period},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("code") != "0" or not data.get("data"):
                        return None
                    items = data["data"]
                    latest = items[0]
                    week_ago = items[6] if len(items) >= 7 else items[-1]
                    return {
                        "ccy": ccy,
                        "current_ratio": float(latest[1]),
                        "week_ago_ratio": float(week_ago[1]),
                        "timestamp": int(latest[0]),
                    }
        except Exception as e:
            logger.warning(f"[OKXRubik] 多空比拉取失败: {e}")
            return None

    async def fetch_oi_history(self, ccy: str = "BTC", period: str = "1D", days: int = 30) -> list[dict]:
        """OI 历史变化（用于计算趋势）"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.BASE}/rubik/stat/contracts/open-interest-volume",
                    params={"ccy": ccy, "period": period},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    data = await resp.json()
                    if data.get("code") != "0" or not data.get("data"):
                        return []
                    items = data["data"][:days]
                    return [
                        {
                            "timestamp": int(it[0]),
                            "oi": float(it[1]),
                            "volume": float(it[2]),
                        }
                        for it in items
                    ]
        except Exception as e:
            logger.warning(f"[OKXRubik] OI历史拉取失败: {e}")
            return []

    async def fetch_oi_change(self, ccy: str = "BTC") -> dict:
        """计算 OI 周变化率"""
        history = await self.fetch_oi_history(ccy, "1D", 8)
        if len(history) < 7:
            return None
        latest_oi = history[0]["oi"]
        week_ago_oi = history[6]["oi"]
        change_pct = (latest_oi - week_ago_oi) / week_ago_oi * 100 if week_ago_oi > 0 else 0
        return {
            "ccy": ccy,
            "current_oi": latest_oi,
            "week_ago_oi": week_ago_oi,
            "week_change_pct": change_pct,
        }
