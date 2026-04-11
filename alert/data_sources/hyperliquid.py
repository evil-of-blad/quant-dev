"""
Hyperliquid 链上数据源
- BTC 永续合约状态（OI / funding / premium / volume）
- premium = 标记价对预言机的偏离度，反映杠杆情绪
"""
import aiohttp
from loguru import logger


class HyperliquidSource:
    BASE = "https://api.hyperliquid.xyz/info"

    async def fetch_btc_state(self) -> dict:
        """
        BTC 当前完整状态
        返回:
        {
            "funding_rate": 0.0000024824,    # 当前 1h 资金费率
            "open_interest": 28139.82,       # OI（BTC 数量）
            "open_interest_usd": ...,        # OI 美元价值
            "premium": -0.0005,              # 期货溢价/贴水
            "mark_price": 73014.0,
            "oracle_price": 73055.0,
            "day_volume_usd": 2390472566,    # 24h 成交额
            "day_change_pct": ...,           # 24h 涨跌
        }
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.BASE,
                    json={"type": "metaAndAssetCtxs"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    data = await resp.json()
                    if not isinstance(data, list) or len(data) < 2:
                        return None

                    universe = data[0].get("universe", [])
                    ctxs = data[1]

                    # 找 BTC
                    btc_idx = None
                    for i, asset in enumerate(universe):
                        if asset.get("name") == "BTC":
                            btc_idx = i
                            break
                    if btc_idx is None or btc_idx >= len(ctxs):
                        return None

                    ctx = ctxs[btc_idx]
                    mark = float(ctx.get("markPx", 0) or 0)
                    oi = float(ctx.get("openInterest", 0) or 0)
                    prev = float(ctx.get("prevDayPx", 0) or 0)
                    day_change_pct = ((mark - prev) / prev * 100) if prev > 0 else 0

                    return {
                        "funding_rate": float(ctx.get("funding", 0) or 0),
                        "open_interest": oi,
                        "open_interest_usd": oi * mark,
                        "premium": float(ctx.get("premium", 0) or 0),
                        "mark_price": mark,
                        "oracle_price": float(ctx.get("oraclePx", 0) or 0),
                        "day_volume_usd": float(ctx.get("dayNtlVlm", 0) or 0),
                        "day_change_pct": day_change_pct,
                    }
        except Exception as e:
            logger.warning(f"[Hyperliquid] BTC state 失败: {e}")
            return None

    async def fetch_btc_oi_history(self, days: int = 7) -> list[dict]:
        """
        BTC OI 历史（用于计算变化率）
        Hyperliquid 提供 candles 接口
        """
        try:
            import time
            now_ms = int(time.time() * 1000)
            start_ms = now_ms - days * 86400 * 1000

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.BASE,
                    json={
                        "type": "candleSnapshot",
                        "req": {
                            "coin": "BTC",
                            "interval": "1d",
                            "startTime": start_ms,
                            "endTime": now_ms,
                        },
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    candles = await resp.json()
                    if not isinstance(candles, list):
                        return []
                    return [
                        {
                            "timestamp": int(c.get("t", 0)),
                            "close": float(c.get("c", 0) or 0),
                            "volume": float(c.get("v", 0) or 0),
                        }
                        for c in candles
                    ]
        except Exception as e:
            logger.warning(f"[Hyperliquid] OI 历史失败: {e}")
            return []
