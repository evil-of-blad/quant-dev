"""
Telegram Bot 指令处理
- /status  查看持仓和权益
- /signal  查看当前策略信号
- /pnl     查看累计盈亏
"""
import asyncio
import aiohttp
from datetime import datetime
from loguru import logger


class TelegramBot:
    """轮询 Telegram getUpdates，处理用户指令"""

    def __init__(self, config: dict, trader):
        tg_cfg = config.get("telegram", {})
        self.enabled: bool = tg_cfg.get("enabled", False)
        self.token: str = tg_cfg.get("bot_token", "")
        self.chat_id: str = str(tg_cfg.get("chat_id", ""))
        self.trader = trader  # LiveTrader 实例，用于读取状态
        self._api_base = f"https://api.telegram.org/bot{self.token}"
        self._last_update_id: int = 0
        self._trade_log: list[dict] = []  # 累计交易记录

    def record_trade(self, symbol: str, direction: str, pnl: float, reason: str):
        """记录一笔交易，供 /pnl 查询"""
        self._trade_log.append({
            "time": datetime.utcnow().strftime("%m-%d %H:%M"),
            "symbol": symbol,
            "direction": direction,
            "pnl": pnl,
            "reason": reason,
        })

    async def start_polling(self):
        """后台轮询 Telegram 消息"""
        if not self.enabled:
            return
        logger.info("[TG Bot] 指令监听已启动")
        while True:
            try:
                await self._poll()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("[TG Bot] 轮询异常: " + str(e))
            await asyncio.sleep(3)

    async def _poll(self):
        url = f"{self._api_base}/getUpdates"
        params = {"offset": self._last_update_id + 1, "timeout": 5}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()

        for update in data.get("result", []):
            self._last_update_id = update["update_id"]
            msg = update.get("message", {})
            text = msg.get("text", "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))

            # 只响应配置的 chat_id
            if chat_id != self.chat_id:
                continue

            if text.startswith("/"):
                await self._handle_command(text)

    async def _handle_command(self, text: str):
        cmd = text.split()[0].lower().split("@")[0]  # 去掉 @botname 后缀

        if cmd == "/status":
            await self._cmd_status()
        elif cmd == "/signal":
            await self._cmd_signal()
        elif cmd == "/pnl":
            await self._cmd_pnl()
        elif cmd == "/help":
            await self._cmd_help()
        else:
            await self._send(f"未知指令: <code>{cmd}</code>\n发送 /help 查看可用指令")

    # ------------------------------------------------------------------
    # /status
    # ------------------------------------------------------------------
    async def _cmd_status(self):
        try:
            balance = await self.trader.exchange.fetch_balance()
            usdt_free = balance.get("USDT", {}).get("free", 0) or 0
            usdt_total = balance.get("USDT", {}).get("total", 0) or 0

            pos_lines = ""
            positions = self.trader._positions
            if positions:
                for sym, pos in positions.items():
                    try:
                        ticker = await self.trader.exchange.fetch_ticker(sym)
                        price = ticker.get("last", pos["avg_price"])
                    except Exception:
                        price = pos["avg_price"]

                    if pos["direction"] == "long":
                        pnl = (price - pos["avg_price"]) * pos["amount"] * self.trader.leverage
                    else:
                        pnl = (pos["avg_price"] - price) * pos["amount"] * self.trader.leverage

                    margin = pos["amount"] * pos["avg_price"] / self.trader.leverage
                    emoji = "📈" if pnl >= 0 else "📉"
                    pos_lines += (
                        f"\n{emoji} <b>{sym}</b>"
                        f"\n   方向: {'多' if pos['direction'] == 'long' else '空'} | 杠杆: {self.trader.leverage}x"
                        f"\n   数量: <code>{pos['amount']:.4f}</code>"
                        f"\n   开仓价: <code>{pos['avg_price']:.2f}</code> | 现价: <code>{price:.2f}</code>"
                        f"\n   保证金: <code>{margin:.2f}</code> | 浮动盈亏: <code>{pnl:+.2f}</code>"
                        f"\n"
                    )
            else:
                pos_lines = "\n空仓，等待信号中..."

            msg = (
                f"📊 <b>账户状态</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"可用余额: <code>{usdt_free:.2f} USDT</code>\n"
                f"账户总额: <code>{usdt_total:.2f} USDT</code>\n"
                f"\n<b>当前持仓:</b>{pos_lines}"
            )
            await self._send(msg)
        except Exception as e:
            await self._send(f"🚨 查询失败: <code>{str(e)[:200]}</code>")

    # ------------------------------------------------------------------
    # /signal
    # ------------------------------------------------------------------
    async def _cmd_signal(self):
        try:
            from core.data_feed import add_indicators
            lines = []

            for symbol in self.trader.symbols:
                df = await self.trader.exchange.fetch_ohlcv(symbol, self.trader.timeframe, limit=300)
                df = add_indicators(df)

                last = df.iloc[-1]
                close = last["close"]
                bb_upper = last.get("bb_upper", 0)
                bb_mid = last.get("bb_mid", 0)
                bb_lower = last.get("bb_lower", 0)
                rsi = last.get("rsi_14", 50)
                atr = last.get("atr_14", 0)

                signal = self.trader.strategy.generate_signal(df, symbol)
                if signal == 1:
                    sig_text = "🟢 做多信号!"
                elif signal == -1:
                    sig_text = "🔴 做空信号!"
                else:
                    sig_text = "⚪ 无信号"

                dist_lower = (close - bb_lower) / close * 100 if close > 0 else 0
                dist_upper = (bb_upper - close) / close * 100 if close > 0 else 0

                lines.append(
                    f"<b>{symbol}</b> {sig_text}\n"
                    f"  价格: <code>{close:.2f}</code>\n"
                    f"  布林上轨: <code>{bb_upper:.2f}</code> (距 {dist_upper:.1f}%)\n"
                    f"  布林中轨: <code>{bb_mid:.2f}</code>\n"
                    f"  布林下轨: <code>{bb_lower:.2f}</code> (距 {dist_lower:.1f}%)\n"
                    f"  RSI: <code>{rsi:.1f}</code> | ATR: <code>{atr:.2f}</code>"
                )

            msg = f"📡 <b>策略信号 ({self.trader.timeframe})</b>\n━━━━━━━━━━━━━━━\n" + "\n\n".join(lines)
            await self._send(msg)
        except Exception as e:
            await self._send(f"🚨 查询失败: <code>{str(e)[:200]}</code>")

    # ------------------------------------------------------------------
    # /pnl
    # ------------------------------------------------------------------
    async def _cmd_pnl(self):
        if not self._trade_log:
            await self._send("📋 <b>交易记录</b>\n\n暂无交易记录，等待第一笔交易...")
            return

        total_pnl = sum(t["pnl"] for t in self._trade_log)
        wins = [t for t in self._trade_log if t["pnl"] > 0]
        losses = [t for t in self._trade_log if t["pnl"] < 0]
        win_rate = len(wins) / len(self._trade_log) * 100 if self._trade_log else 0

        # 最近 10 笔
        recent = self._trade_log[-10:]
        trade_lines = ""
        for t in recent:
            emoji = "✅" if t["pnl"] >= 0 else "❌"
            d = "多" if t["direction"] == "long" else "空"
            trade_lines += f"\n{emoji} {t['time']} | {t['symbol']} {d} | <code>{t['pnl']:+.2f}</code> | {t['reason']}"

        msg = (
            f"📋 <b>盈亏统计</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"总交易: <code>{len(self._trade_log)}</code> 笔\n"
            f"胜/负: <code>{len(wins)}/{len(losses)}</code> (胜率 {win_rate:.0f}%)\n"
            f"累计盈亏: <code>{total_pnl:+.2f} USDT</code>\n"
            f"\n<b>最近交易:</b>{trade_lines}"
        )
        await self._send(msg)

    # ------------------------------------------------------------------
    # /help
    # ------------------------------------------------------------------
    async def _cmd_help(self):
        msg = (
            "🤖 <b>量化机器人指令</b>\n"
            "━━━━━━━━━━━━━━━\n"
            "/status — 查看持仓和权益\n"
            "/signal — 查看当前策略信号\n"
            "/pnl — 查看累计盈亏\n"
            "/help — 显示帮助"
        )
        await self._send(msg)

    async def _send(self, message: str):
        try:
            url = f"{self._api_base}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"[TG Bot] 发送失败: {body}")
        except Exception as e:
            logger.warning("[TG Bot] 发送异常: " + str(e))
