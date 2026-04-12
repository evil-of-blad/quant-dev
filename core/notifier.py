"""
Telegram 通知模块
- 开仓/平仓/止损/止盈/熔断 推送到手机
- 定时权益播报
"""
import aiohttp
from loguru import logger


class TelegramNotifier:

    def __init__(self, config: dict):
        tg_cfg = config.get("telegram", {})
        self.enabled: bool = tg_cfg.get("enabled", False)
        self.token: str = tg_cfg.get("bot_token", "")
        self.chat_id: str = str(tg_cfg.get("chat_id", ""))
        self._api_url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        self._session: aiohttp.ClientSession = None  # 复用 session 避免连接堆积

        if self.enabled and (not self.token or not self.chat_id):
            logger.warning("[TG] enabled=true 但 bot_token 或 chat_id 为空，通知将不会发送")
            self.enabled = False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=2, force_close=True, enable_cleanup_closed=True)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def send(self, message: str):
        """发送 Telegram 消息"""
        if not self.enabled:
            return
        try:
            session = await self._get_session()
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            async with session.post(self._api_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"[TG] 发送失败 status={resp.status}: {body}")
        except Exception as e:
            logger.warning("[TG] 发送异常: " + str(e))

    # ------------------------------------------------------------------
    # 快捷方法
    # ------------------------------------------------------------------
    async def notify_open(self, symbol: str, direction: str, amount: float,
                          price: float, leverage: int, margin: float):
        d = "多" if direction == "long" else "空"
        coin = symbol.split("/")[0]
        await self.send(f"{'🟢' if direction == 'long' else '🔴'} 开{d} {coin} {amount:.4f} @ {price:.2f} | {leverage}x 保证金 {margin:.0f}U")

    async def notify_close(self, symbol: str, direction: str, price: float,
                           pnl: float, reason: str):
        coin = symbol.split("/")[0]
        await self.send(f"{'✅' if pnl >= 0 else '❌'} 平仓 {coin} @ {price:.2f} | {pnl:+.2f}U | {reason}")

    async def notify_stop_loss(self, symbol: str, direction: str, price: float, pnl: float):
        await self.notify_close(symbol, direction, price, pnl, "止损")

    async def notify_take_profit(self, symbol: str, direction: str, price: float, pnl: float):
        await self.notify_close(symbol, direction, price, pnl, "止盈")

    async def notify_trailing_stop(self, symbol: str, direction: str, price: float, pnl: float):
        await self.notify_close(symbol, direction, price, pnl, "移动止盈")

    async def notify_circuit_breaker(self, equity: float, drawdown_pct: float):
        await self.send(f"⚠️ 熔断 | 权益 {equity:.0f}U | 回撤 {drawdown_pct:.1%} | 已暂停，冷却后恢复")

    async def notify_status(self, equity: float, cash: float,
                            positions: dict, prices: dict):
        lines = [f"📊 权益 {equity:.0f}U | 可用 {cash:.0f}U"]
        if positions:
            for sym, pos in positions.items():
                price = prices.get(sym, pos["avg_price"])
                d = "多" if pos["direction"] == "long" else "空"
                if pos["direction"] == "long":
                    pnl = (price - pos["avg_price"]) * pos["amount"]
                else:
                    pnl = (pos["avg_price"] - price) * pos["amount"]
                coin = sym.split("/")[0]
                lines.append(f"  {coin} {d} @ {pos['avg_price']:.2f} → {price:.2f} ({pnl:+.2f}U)")
        else:
            lines.append("  空仓")
        await self.send("\n".join(lines))

    async def notify_startup(self, strategy: str, symbols: list,
                             leverage: int, equity: float):
        coins = [s.split("/")[0] for s in symbols]
        await self.send(f"🚀 启动 | {strategy} | {'+'.join(coins)} {leverage}x | {equity:.0f}U")

    async def notify_error(self, error: str):
        await self.send(f"🚨 {str(error)[:300]}")
