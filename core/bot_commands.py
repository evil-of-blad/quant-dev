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
        elif cmd == "/report":
            await self._cmd_report()
        elif cmd == "/alert":
            await self._cmd_alert()
        elif cmd == "/help":
            await self._cmd_help()
        else:
            await self._send(f"未知指令: {cmd}\n发送 /help 查看可用指令")

    # ------------------------------------------------------------------
    # /status — 账户 + 持仓状态
    # ------------------------------------------------------------------
    async def _cmd_status(self):
        try:
            ex = self.trader.exchange
            balance = await ex.fetch_balance()
            usdt = balance.get("USDT", {})
            free = usdt.get("free", 0) or 0
            total = usdt.get("total", 0) or 0

            symbols = self.trader.config.get("trading", {}).get("symbols", [])

            # 拉取合约持仓
            try:
                all_positions = await ex.client.fetch_positions(symbols)
            except Exception:
                all_positions = []

            pos_lines = []
            for p in all_positions:
                sym = p.get("symbol")
                contracts = float(p.get("contracts", 0) or 0)
                if contracts < 1e-9:
                    continue
                coin = sym.split("/")[0]
                d = "多" if p.get("side") == "long" else "空"
                entry = float(p.get("entryPrice", 0) or 0)
                lev = int(float(p.get("leverage", 1) or 1))
                upnl = float(p.get("unrealizedPnl", 0) or 0)
                pos_lines.append(f"{'📈' if upnl >= 0 else '📉'} {coin} {d} {contracts}张 @ {entry:.2f} | {lev}x | {upnl:+.2f}U")

            if not pos_lines:
                pos_lines.append("空仓")

            used_margin = self.trader._used_margin()
            msg = (
                f"📊 总额 {total:.0f}U | 可用 {free:.0f}U | 保证金 {used_margin:.0f}U\n"
                + "\n".join(pos_lines)
            )
            await self._send(msg)
        except Exception as e:
            await self._send(f"🚨 查询失败: {str(e)[:200]}")

    # ------------------------------------------------------------------
    # /signal — MA 交叉 + 趋势过滤状态
    # ------------------------------------------------------------------
    async def _cmd_signal(self):
        try:
            from core.data_feed import add_indicators
            params = self.trader.config.get("strategy", {}).get("params", {})
            fast_p = params.get("fast_period", 15)
            slow_p = params.get("slow_period", 50)
            trend_p = params.get("trend_period", 200)

            lines = []
            for symbol in self.trader.symbols:
                df = await self.trader.exchange.fetch_ohlcv(symbol, self.trader.timeframe, limit=500)
                df = add_indicators(df)
                price = float(df["close"].iloc[-1])

                fast_col = f"sma_{fast_p}"
                slow_col = f"sma_{slow_p}"
                trend_col = f"sma_{trend_p}"
                fast_ma = float(df[fast_col].iloc[-1]) if fast_col in df.columns else 0
                slow_ma = float(df[slow_col].iloc[-1]) if slow_col in df.columns else 0
                sma200 = float(df[trend_col].iloc[-1]) if trend_col in df.columns else 0

                signal = self.trader.strategy.generate_signal(df, symbol)
                if signal == 1:
                    sig = "🟢 做多"
                elif signal == -1:
                    sig = "🔴 做空"
                else:
                    sig = "⚪ 无"

                coin = symbol.split("/")[0]
                trend = "上方" if price > sma200 else "下方"
                cross = "金叉" if fast_ma > slow_ma else "死叉"
                gap = (price - sma200) / sma200 * 100 if sma200 > 0 else 0

                lines.append(f"{coin} {sig} | {cross} | SMA200{trend}({gap:+.1f}%)")

            msg = f"📡 MA{fast_p}/{slow_p} + SMA{trend_p}\n" + "\n".join(lines)
            await self._send(msg)
        except Exception as e:
            await self._send(f"🚨 查询失败: {str(e)[:200]}")

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
        """显示账户余额和资金分配"""
        try:
            balance = await self.trader.exchange.fetch_balance()
            usdt = balance.get("USDT", {})
            total = usdt.get("total", 0) or 0
            free = usdt.get("free", 0) or 0

            alloc_cfg = self.trader.config.get("allocation", {})
            bp = alloc_cfg.get("bollinger_pct", 0)
            rp = max(0, 1 - bp)

            current_alloc = self.trader.allocated_capital
            used_margin = self.trader._used_margin()
            avail = max(0, current_alloc - used_margin)

            msg = (
                f"💰 总额 {total:.0f}U | 可用 {free:.0f}U\n"
                f"策略分配 {current_alloc:.0f}U ({bp:.0%}) | 保证金 {used_margin:.0f}U | 剩余 {avail:.0f}U\n"
                f"缓冲 {total*rp:.0f}U ({rp:.0%})"
            )

            expected = total * bp
            if abs(expected - current_alloc) > 10:
                msg += f"\n⚠️ 余额变动 {expected - current_alloc:+.0f}U，发 /realloc 重新分配"

            await self._send(msg)
        except Exception as e:
            await self._send(f"🚨 查询失败: {str(e)[:200]}")

    # ------------------------------------------------------------------
    # /realloc
    # ------------------------------------------------------------------
    async def _cmd_realloc(self):
        """重新分配策略资金"""
        try:
            old = self.trader.allocated_capital
            await self.trader.allocator.init(self.trader.exchange)
            new = self.trader.allocator.get("bollinger")
            self.trader.allocated_capital = new
            change = new - old
            total = self.trader.allocator.total_balance

            await self._send(f"🔄 重新分配 {old:.0f} → {new:.0f}U ({change:+.0f}U) | 总额 {total:.0f}U")
        except Exception as e:
            await self._send(f"🚨 重新分配失败: {str(e)[:200]}")

    # ------------------------------------------------------------------
    # /report — 跨策略统计日报
    # ------------------------------------------------------------------
    async def _cmd_report(self):
        try:
            from core.stats import get_all_summaries
            summaries = get_all_summaries()
            if not summaries:
                await self._send("📋 暂无策略统计数据")
                return

            total_pnl = sum(s["total_pnl"] for s in summaries)
            total_today = sum(s["today_pnl"] for s in summaries)
            total_trades = sum(s["total_trades"] for s in summaries)

            lines = []
            for s in summaries:
                pnl_emoji = "📈" if s["total_pnl"] >= 0 else "📉"
                today_emoji = "🟢" if s["today_pnl"] >= 0 else "🔴"
                lines.append(
                    f"\n{pnl_emoji} <b>{s['strategy']}</b>"
                    f"\n  累计盈亏: <code>{s['total_pnl']:+.2f}</code>"
                    f"\n  今日: {today_emoji} <code>{s['today_pnl']:+.2f}</code>"
                    f"\n  交易: {s['total_trades']} 笔 | 胜率 {s['win_rate']:.0%}"
                    f"\n  最大单笔: 盈 {s['max_pnl']:+.2f} / 亏 {s['max_loss']:+.2f}"
                )

            msg = (
                f"📊 <b>策略统计日报</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"<b>总览</b>\n"
                f"  累计盈亏: <code>{total_pnl:+.2f} USDT</code>\n"
                f"  今日盈亏: <code>{total_today:+.2f} USDT</code>\n"
                f"  总交易数: {total_trades}\n"
                + "".join(lines)
            )
            await self._send(msg)
        except Exception as e:
            await self._send(f"🚨 查询失败: <code>{str(e)[:200]}</code>")

    # ------------------------------------------------------------------
    # /help
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # /alert — 实时市场指标分析
    # ------------------------------------------------------------------
    async def _cmd_alert(self):
        """拉取 9 个市场指标，实时评分返回"""
        try:
            await self._send("⏳ 正在采集 9 项指标...")

            from alert.data_sources.fear_greed import FearGreedSource
            from alert.data_sources.coingecko import CoinGeckoSource
            from alert.data_sources.defillama import DefiLlamaSource
            from alert.data_sources.okx_metrics import OKXMetricsSource
            from alert.data_sources.okx_rubik import OKXRubikSource
            from alert.data_sources.hyperliquid import HyperliquidSource
            from alert import indicators
            from alert.alert_engine import INDICATOR_WEIGHTS

            fg_src = FearGreedSource()
            cg_src = CoinGeckoSource()
            dl_src = DefiLlamaSource()
            okx_src = OKXMetricsSource()
            rubik_src = OKXRubikSource()
            hl_src = HyperliquidSource()

            scores = []
            errors = []

            # 并行拉取所有数据源
            async def safe_fetch(name, coro):
                try:
                    return await coro
                except Exception as e:
                    errors.append(f"{name}: {e}")
                    return None

            fg, ma, fr, cg, sc, oi, ls, hl = await asyncio.gather(
                safe_fetch("恐贪", fg_src.fetch()),
                safe_fetch("MA200", okx_src.fetch_ma200_deviation("BTC/USDT:USDT")),
                safe_fetch("费率", okx_src.fetch_funding_rate("BTC/USDT:USDT")),
                safe_fetch("市值", cg_src.fetch_global()),
                safe_fetch("稳定币", dl_src.fetch_stablecoins()),
                safe_fetch("OI", rubik_src.fetch_oi_change("BTC")),
                safe_fetch("多空比", rubik_src.fetch_long_short_ratio("BTC", "1D")),
                safe_fetch("HL", hl_src.fetch_btc_state()),
            )

            if fg:
                scores.append(indicators.score_fear_greed(fg["value"]))
            if ma:
                scores.append(indicators.score_ma200_deviation(ma["deviation_pct"]))
            if fr:
                scores.append(indicators.score_funding_rate(fr["rate"]))
            if cg:
                scores.append(indicators.score_btc_dominance(cg["btc_dominance"]))
            if sc:
                scores.append(indicators.score_stablecoin_change(sc["week_change_pct"]))
            if oi:
                scores.append(indicators.score_oi_change(oi["week_change_pct"]))
            if ls:
                scores.append(indicators.score_long_short_ratio(ls["current_ratio"]))
            if hl:
                scores.append(indicators.score_hl_premium(hl["premium"]))
                scores.append(indicators.score_hl_funding(hl["funding_rate"]))

            await okx_src.close()

            if not scores:
                await self._send(f"🚨 所有指标拉取失败\n{chr(10).join(errors)}")
                return

            # 综合评分
            bull_total = bear_total = total_w = 0
            for s in scores:
                w = INDICATOR_WEIGHTS.get(s.name, 1.0)
                total_w += w
                if "bullish" in s.direction:
                    bull_total += s.score * w
                elif "bearish" in s.direction:
                    bear_total += s.score * w

            bull_avg = bull_total / total_w if total_w > 0 else 0
            bear_avg = bear_total / total_w if total_w > 0 else 0

            if bull_avg > bear_avg:
                total_score, direction = bull_avg, "bullish"
            else:
                total_score, direction = bear_avg, "bearish"

            # 分级
            if total_score < 30:
                level = "正常"
            elif total_score < 50:
                level = "关注"
            elif total_score < 70:
                level = "⚠️ WARNING"
            elif total_score < 85:
                level = "🔴 CRITICAL"
            else:
                level = "⚫ EMERGENCY"

            dir_text = "📈看多(接近底部)" if direction == "bullish" else "📉看空(接近顶部)"

            # 格式化输出
            lines = [f"📡 市场分析 | {level} | {total_score:.0f}分 | {dir_text}"]
            lines.append("")

            # 按 score 排序，高分在前
            for s in sorted(scores, key=lambda x: x.score, reverse=True):
                if s.score >= 70:
                    tag = "🔴"
                elif s.score >= 40:
                    tag = "🟡"
                else:
                    tag = "🟢"
                lines.append(f"{tag} {s.description}")

            if errors:
                lines.append(f"\n⚠️ {len(errors)}项失败: {', '.join(e.split(':')[0] for e in errors)}")

            await self._send("\n".join(lines))
        except Exception as e:
            await self._send(f"🚨 告警查询失败: {str(e)[:300]}")

    async def _cmd_help(self):
        await self._send(
            "/status — 持仓+权益\n"
            "/signal — MA 信号状态\n"
            "/alert — 市场 9 指标分析\n"
            "/pnl — 盈亏统计\n"
            "/balance — 余额+分配\n"
            "/realloc — 重新分配资金\n"
            "/report — 策略日报"
        )

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
