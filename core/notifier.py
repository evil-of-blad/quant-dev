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
        emoji = "🟢" if direction == "long" else "🔴"
        msg = (
            f"{emoji} <b>开{'多' if direction == 'long' else '空'}仓</b>\n"
            f"标的: <code>{symbol}</code>\n"
            f"数量: <code>{amount:.4f}</code>\n"
            f"价格: <code>{price:.2f}</code>\n"
            f"杠杆: {leverage}x\n"
            f"保证金: <code>{margin:.2f} USDT</code>"
        )
        await self.send(msg)

    async def notify_close(self, symbol: str, direction: str, price: float,
                           pnl: float, reason: str):
        emoji = "✅" if pnl >= 0 else "❌"
        msg = (
            f"{emoji} <b>平仓 — {reason}</b>\n"
            f"标的: <code>{symbol}</code>\n"
            f"方向: {'多' if direction == 'long' else '空'}\n"
            f"价格: <code>{price:.2f}</code>\n"
            f"盈亏: <code>{pnl:+.2f} USDT</code>"
        )
        await self.send(msg)

    async def notify_stop_loss(self, symbol: str, direction: str, price: float, pnl: float):
        await self.notify_close(symbol, direction, price, pnl, "止损 🛑")

    async def notify_take_profit(self, symbol: str, direction: str, price: float, pnl: float):
        await self.notify_close(symbol, direction, price, pnl, "止盈 🎯")

    async def notify_trailing_stop(self, symbol: str, direction: str, price: float, pnl: float):
        await self.notify_close(symbol, direction, price, pnl, "移动止盈 📈")

    async def notify_circuit_breaker(self, equity: float, drawdown_pct: float):
        msg = (
            f"⚠️ <b>熔断触发</b>\n"
            f"当前权益: <code>{equity:.2f} USDT</code>\n"
            f"回撤: <code>{drawdown_pct:.2%}</code>\n"
            f"已暂停交易，冷却后自动恢复"
        )
        await self.send(msg)

    async def notify_status(self, equity: float, cash: float,
                            positions: dict, prices: dict):
        pos_lines = ""
        if positions:
            for sym, pos in positions.items():
                price = prices.get(sym, pos["avg_price"])
                direction = pos["direction"]
                if direction == "long":
                    pnl = (price - pos["avg_price"]) * pos["amount"]
                else:
                    pnl = (pos["avg_price"] - price) * pos["amount"]
                emoji = "📈" if pnl >= 0 else "📉"
                pos_lines += (
                    f"\n{emoji} {sym}: {'多' if direction == 'long' else '空'} "
                    f"{pos['amount']:.4f} @ {pos['avg_price']:.2f} "
                    f"| 浮动: {pnl:+.2f}"
                )
        else:
            pos_lines = "\n空仓"

        msg = (
            f"📊 <b>定时播报</b>\n"
            f"权益: <code>{equity:.2f} USDT</code>\n"
            f"可用: <code>{cash:.2f} USDT</code>\n"
            f"<b>持仓:</b>{pos_lines}"
        )
        await self.send(msg)

    async def notify_startup(self, strategy: str, symbols: list,
                             leverage: int, equity: float):
        msg = (
            f"🚀 <b>量化机器人启动</b>\n"
            f"策略: <code>{strategy}</code>\n"
            f"标的: <code>{', '.join(symbols)}</code>\n"
            f"杠杆: {leverage}x\n"
            f"初始权益: <code>{equity:.2f} USDT</code>"
        )
        await self.send(msg)

    async def notify_error(self, error: str):
        msg = f"🚨 <b>异常报警</b>\n<code>{error[:500]}</code>"
        await self.send(msg)
