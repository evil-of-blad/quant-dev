"""
告警引擎
- 定时拉取所有指标
- 加权综合打分
- 触发不同等级告警 → Telegram 推送
- 告警限频，避免刷屏
"""
import asyncio
import json
import os
from datetime import datetime, timezone
from enum import Enum
from loguru import logger

from core.notifier import TelegramNotifier
from .data_sources.fear_greed import FearGreedSource
from .data_sources.coingecko import CoinGeckoSource
from .data_sources.defillama import DefiLlamaSource
from .data_sources.okx_metrics import OKXMetricsSource
from . import indicators


class AlertLevel(str, Enum):
    NORMAL = "normal"        # 不通知
    INFO = "info"            # 每日汇总
    WARNING = "warning"      # 立即通知
    CRITICAL = "critical"    # 立即 + 6h 提醒
    EMERGENCY = "emergency"  # 持续推送


# 指标权重
INDICATOR_WEIGHTS = {
    "fear_greed": 1.5,
    "ma200_deviation": 1.5,
    "funding_rate": 1.0,
    "btc_dominance": 0.8,
    "stablecoin_change": 1.2,
}


class AlertEngine:

    def __init__(self, config: dict):
        self.config = config
        self.notifier = TelegramNotifier(config)

        alert_cfg = config.get("alert", {})
        self.check_interval: int = alert_cfg.get("check_interval", 1800)  # 30 分钟
        self.warning_repeat_hours: int = alert_cfg.get("warning_repeat_hours", 3)
        self.critical_repeat_hours: int = alert_cfg.get("critical_repeat_hours", 6)
        self.daily_report_hour: int = alert_cfg.get("daily_report_hour", 22)  # UTC 时间

        # 数据源
        self.fear_greed = FearGreedSource()
        self.coingecko = CoinGeckoSource()
        self.defillama = DefiLlamaSource()
        self.okx = OKXMetricsSource()

        # 告警历史
        self._history_file = "logs/alert_history.json"
        self._last_alert_time: dict = self._load_history()  # level -> ISO timestamp
        self._last_daily_report_date: str = None

        self._running = False

    def _load_history(self) -> dict:
        if os.path.exists(self._history_file):
            try:
                with open(self._history_file) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_history(self):
        try:
            os.makedirs(os.path.dirname(self._history_file), exist_ok=True)
            with open(self._history_file, "w") as f:
                json.dump(self._last_alert_time, f, indent=2)
        except Exception as e:
            logger.warning(f"[Alert] 保存历史失败: {e}")

    async def start(self):
        logger.info(f"[告警引擎] 启动，检查间隔 {self.check_interval}s")
        await self.notifier.send(
            f"📡 <b>市场告警引擎启动</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"监控指标: 5 个核心指标\n"
            f"检查间隔: {self.check_interval}s\n"
            f"日报时间: 每日 {self.daily_report_hour}:00 UTC"
        )
        self._running = True
        try:
            await self._main_loop()
        finally:
            await self.okx.close()

    async def stop(self):
        self._running = False
        await self.okx.close()

    async def _main_loop(self):
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[告警引擎] 异常: {e}", exc_info=True)
            await asyncio.sleep(self.check_interval)

    async def _tick(self):
        # 1. 拉取所有指标
        scores = await self._collect_all_scores()

        # 2. 综合评分
        total_score, direction = self._calc_total_score(scores)
        level = self._classify_level(total_score)

        logger.info(
            f"[告警引擎] 综合评分 {total_score:.1f} ({level.value}, {direction}) | "
            f"指标数: {len(scores)}"
        )

        # 3. 决定是否推送
        await self._maybe_push_alert(level, total_score, direction, scores)

        # 4. 每日定时日报（UTC）
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if now.hour == self.daily_report_hour and self._last_daily_report_date != today:
            await self._send_daily_report(total_score, direction, scores)
            self._last_daily_report_date = today

    async def _collect_all_scores(self) -> list[indicators.IndicatorScore]:
        scores = []

        # 恐贪
        try:
            fg = await self.fear_greed.fetch()
            if fg:
                scores.append(indicators.score_fear_greed(fg["value"]))
        except Exception as e:
            logger.warning(f"[Alert] fear_greed 失败: {e}")

        # 200 周均线偏离
        try:
            ma = await self.okx.fetch_ma200_deviation("BTC/USDT:USDT")
            if ma:
                scores.append(indicators.score_ma200_deviation(ma["deviation_pct"]))
        except Exception as e:
            logger.warning(f"[Alert] ma200 失败: {e}")

        # 资金费率
        try:
            fr = await self.okx.fetch_funding_rate("BTC/USDT:USDT")
            if fr:
                scores.append(indicators.score_funding_rate(fr["rate"]))
        except Exception as e:
            logger.warning(f"[Alert] funding_rate 失败: {e}")

        # BTC 主导率
        try:
            cg = await self.coingecko.fetch_global()
            if cg:
                scores.append(indicators.score_btc_dominance(cg["btc_dominance"]))
        except Exception as e:
            logger.warning(f"[Alert] dominance 失败: {e}")

        # 稳定币市值
        try:
            sc = await self.defillama.fetch_stablecoins()
            if sc:
                scores.append(indicators.score_stablecoin_change(sc["week_change_pct"]))
        except Exception as e:
            logger.warning(f"[Alert] stablecoin 失败: {e}")

        return scores

    def _calc_total_score(self, scores: list[indicators.IndicatorScore]) -> tuple[float, str]:
        """
        加权汇总，并判断主导方向（bullish 还是 bearish）
        """
        if not scores:
            return 0, "neutral"

        bullish_total = 0
        bearish_total = 0
        total_weight = 0

        for s in scores:
            w = INDICATOR_WEIGHTS.get(s.name, 1.0)
            total_weight += w
            if "bullish" in s.direction:
                bullish_total += s.score * w
            elif "bearish" in s.direction:
                bearish_total += s.score * w

        bullish_avg = bullish_total / total_weight if total_weight > 0 else 0
        bearish_avg = bearish_total / total_weight if total_weight > 0 else 0

        if bullish_avg > bearish_avg:
            return bullish_avg, "bullish"
        else:
            return bearish_avg, "bearish"

    def _classify_level(self, score: float) -> AlertLevel:
        if score < 30:
            return AlertLevel.NORMAL
        elif score < 50:
            return AlertLevel.INFO
        elif score < 70:
            return AlertLevel.WARNING
        elif score < 85:
            return AlertLevel.CRITICAL
        else:
            return AlertLevel.EMERGENCY

    async def _maybe_push_alert(self, level: AlertLevel, score: float,
                                direction: str, scores: list):
        """根据等级和限频决定是否推送"""
        if level == AlertLevel.NORMAL:
            return
        if level == AlertLevel.INFO:
            return  # INFO 只在日报时推送

        now = datetime.now(timezone.utc)
        last_iso = self._last_alert_time.get(level.value)
        if last_iso:
            try:
                last = datetime.fromisoformat(last_iso)
                hours_passed = (now - last).total_seconds() / 3600

                # 限频
                if level == AlertLevel.WARNING and hours_passed < self.warning_repeat_hours:
                    return
                if level == AlertLevel.CRITICAL and hours_passed < self.critical_repeat_hours:
                    return
                # EMERGENCY 不限频
            except Exception:
                pass

        # 推送
        await self._send_alert(level, score, direction, scores)
        self._last_alert_time[level.value] = now.isoformat()
        self._save_history()

    async def _send_alert(self, level: AlertLevel, score: float,
                          direction: str, scores: list):
        emoji_map = {
            AlertLevel.WARNING: "🟡",
            AlertLevel.CRITICAL: "🔴",
            AlertLevel.EMERGENCY: "⚫",
        }
        dir_text = "📈 看多信号" if direction == "bullish" else "📉 看空信号"

        triggered = [s for s in scores if s.score >= 50]
        normal = [s for s in scores if s.score < 50]

        lines = []
        lines.append(f"{emoji_map.get(level, '⚪')} <b>{level.value.upper()} - {dir_text}</b>")
        lines.append(f"━━━━━━━━━━━━━━━")
        lines.append(f"综合评分: <code>{score:.1f}/100</code>")
        lines.append("")
        lines.append(f"<b>触发指标 ({len(triggered)}):</b>")
        for s in triggered:
            lines.append(f"  • {s.description}")
        if normal:
            lines.append("")
            lines.append(f"<b>未触发 ({len(normal)}):</b>")
            for s in normal:
                lines.append(f"  • {s.description}")

        # 建议
        lines.append("")
        if direction == "bullish" and level in [AlertLevel.CRITICAL, AlertLevel.EMERGENCY]:
            lines.append("💡 <b>建议:</b> 可能接近底部，可考虑分批加仓现货 BTC")
        elif direction == "bearish" and level in [AlertLevel.CRITICAL, AlertLevel.EMERGENCY]:
            lines.append("💡 <b>建议:</b> 可能接近顶部，可考虑减仓避险")
        else:
            lines.append("💡 <b>建议:</b> 关注后续走势，等待更明确信号")

        await self.notifier.send("\n".join(lines))

    async def _send_daily_report(self, score: float, direction: str, scores: list):
        """每日 22:00 发送日报"""
        lines = [
            "📊 <b>市场状态日报</b>",
            "━━━━━━━━━━━━━━━",
            f"日期: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            f"综合评分: <code>{score:.1f}/100</code>",
            f"主导方向: {'📈 看多' if direction == 'bullish' else '📉 看空' if direction == 'bearish' else '⚖️ 中性'}",
            "",
            "<b>各指标状态:</b>",
        ]
        for s in scores:
            tag = "🟢" if s.score < 30 else ("🟡" if s.score < 60 else "🔴")
            lines.append(f"  {tag} {s.description}")

        await self.notifier.send("\n".join(lines))
