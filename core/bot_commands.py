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
        self.trader = trader
        self._api_base = f"https://api.telegram.org/bot{self.token}"
        self._last_update_id: int = 0
        self._trade_log: list[dict] = []
        self._session: aiohttp.ClientSession = None  # 复用 session
        self._poll_interval: int = tg_cfg.get("bot_poll_interval", 15)  # 默认 15 秒

    def record_trade(self, symbol: str, direction: str, pnl: float, reason: str):
        """记录一笔交易，供 /pnl 查询"""
        self._trade_log.append({
            "time": datetime.utcnow().strftime("%m-%d %H:%M"),
            "symbol": symbol,
            "direction": direction,
            "pnl": pnl,
            "reason": reason,
        })

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=2, force_close=True, enable_cleanup_closed=True)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def start_polling(self):
        """后台轮询 Telegram 消息"""
        if not self.enabled:
            return
        logger.info(f"[TG Bot] 指令监听已启动 (轮询间隔 {self._poll_interval}s)")
        while True:
            try:
                await self._poll()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("[TG Bot] 轮询异常: " + str(e))
            await asyncio.sleep(self._poll_interval)

    async def _poll(self):
        # 不用 long-polling（timeout=0），改成短连接
        url = f"{self._api_base}/getUpdates"
        params = {"offset": self._last_update_id + 1, "timeout": 0}
        session = await self._get_session()
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
        elif cmd == "/balance":
            await self._cmd_balance()
        elif cmd == "/realloc":
            await self._cmd_realloc()
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
    # /balance
    # ------------------------------------------------------------------
    async def _cmd_balance(self):
        """显示账户余额、资金分配、利用情况"""
        try:
            balance = await self.trader.exchange.fetch_balance()
            usdt = balance.get("USDT", {})
            total = usdt.get("total", 0) or 0
            free = usdt.get("free", 0) or 0
            used = usdt.get("used", 0) or 0

            alloc_cfg = self.trader.config.get("allocation", {})
            bp = alloc_cfg.get("bollinger_pct", 0)
            ap = alloc_cfg.get("funding_arb_pct", 0)
            gp = alloc_cfg.get("grid_pct", 0)
            rp = max(0, 1 - bp - ap - gp)

            current_alloc = self.trader.allocated_capital
            expected_alloc = total * bp
            diff = expected_alloc - current_alloc
            used_margin = self.trader._used_margin()

            msg = (
                f"💰 <b>账户余额 & 资金分配</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"总额: <code>{total:.2f} USDT</code>\n"
                f"可用: <code>{free:.2f} USDT</code>\n"
                f"占用: <code>{used:.2f} USDT</code>\n\n"
                f"<b>按比例应分配（基于实时余额）:</b>\n"
                f"  布林带: <code>{total*bp:.0f}</code> ({bp:.0%})\n"
                f"  套利:   <code>{total*ap:.0f}</code> ({ap:.0%})\n"
                f"  网格:   <code>{total*gp:.0f}</code> ({gp:.0%})\n"
                f"  缓冲:   <code>{total*rp:.0f}</code> ({rp:.0%})\n\n"
                f"<b>布林带实际状态:</b>\n"
                f"  已分配: <code>{current_alloc:.0f}</code> USDT\n"
                f"  已用保证金: <code>{used_margin:.0f}</code> USDT\n"
                f"  本策略可用: <code>{max(0, current_alloc - used_margin):.0f}</code> USDT"
            )

            if abs(diff) > 10:
                msg += (
                    f"\n\n⚠️ 实时余额变化 <code>{diff:+.0f} USDT</code>\n"
                    f"发送 /realloc 重新分配（仅布林带，套利和网格需重启）"
                )

            await self._send(msg)
        except Exception as e:
            await self._send(f"🚨 查询失败: <code>{str(e)[:200]}</code>")

    # ------------------------------------------------------------------
    # /realloc
    # ------------------------------------------------------------------
    async def _cmd_realloc(self):
        """重新分配资金（仅当前策略：布林带）"""
        try:
            old = self.trader.allocated_capital
            await self.trader.allocator.init(self.trader.exchange)
            new = self.trader.allocator.get("bollinger")
            self.trader.allocated_capital = new

            change = new - old
            emoji = "📈" if change > 0 else ("📉" if change < 0 else "➡️")

            msg = (
                f"🔄 <b>布林带资金重新分配</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"原分配: <code>{old:.0f} USDT</code>\n"
                f"新分配: <code>{new:.0f} USDT</code>\n"
                f"变化: {emoji} <code>{change:+.0f} USDT</code>\n\n"
                f"账户总额: <code>{self.trader.allocator.total_balance:.2f} USDT</code>\n\n"
                f"<i>套利和网格策略需单独重启服务才能重新分配:\n"
                f"bash scripts/start_arb.sh restart\n"
                f"bash scripts/start_grid.sh restart</i>"
            )
            await self._send(msg)
        except Exception as e:
            await self._send(f"🚨 重新分配失败: <code>{str(e)[:200]}</code>")

    # ------------------------------------------------------------------
    # /help
    # ------------------------------------------------------------------
    async def _cmd_help(self):
        msg = (
            "🤖 <b>量化机器人指令</b>\n"
            "━━━━━━━━━━━━━━━\n"
            "/status  — 查看持仓和权益\n"
            "/signal  — 查看当前策略信号\n"
            "/pnl     — 查看累计盈亏\n"
            "/balance — 查看账户余额和资金分配\n"
            "/realloc — 重新分配布林带资金\n"
            "/help    — 显示帮助"
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
            session = await self._get_session()
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"[TG Bot] 发送失败: {body}")
        except Exception as e:
            logger.warning("[TG Bot] 发送异常: " + str(e))
