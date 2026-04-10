"""
策略统计模块 — 持久化盈亏、交易记录到 logs/stats/
- 每个策略一个文件
- 支持累计盈亏查询
- 支持每日快照
"""
import json
import os
from datetime import datetime, timezone
from loguru import logger


STATS_DIR = "logs/stats"


class StrategyStats:
    """单个策略的统计数据"""

    def __init__(self, strategy_name: str):
        self.strategy_name = strategy_name
        os.makedirs(STATS_DIR, exist_ok=True)
        self._file = os.path.join(STATS_DIR, f"{strategy_name}.json")
        self.data: dict = self._load()

    def _load(self) -> dict:
        if os.path.exists(self._file):
            try:
                with open(self._file) as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"[Stats] 加载 {self._file} 失败: {e}")
        return {
            "strategy": self.strategy_name,
            "total_trades": 0,
            "win_trades": 0,
            "loss_trades": 0,
            "total_pnl": 0.0,
            "total_fee": 0.0,
            "max_pnl": 0.0,           # 单笔最大盈利
            "max_loss": 0.0,          # 单笔最大亏损
            "started_at": datetime.now(timezone.utc).isoformat(),
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "daily_snapshots": [],    # [{date, equity, pnl}]
            "recent_trades": [],      # 最近 100 笔
        }

    def save(self):
        try:
            self.data["last_updated"] = datetime.now(timezone.utc).isoformat()
            with open(self._file, "w") as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            logger.warning(f"[Stats] 保存失败: {e}")

    def record_trade(self, symbol: str, direction: str, pnl: float, fee: float = 0, reason: str = ""):
        """记录一笔已完成的交易"""
        self.data["total_trades"] += 1
        self.data["total_pnl"] += pnl
        self.data["total_fee"] += fee

        if pnl > 0:
            self.data["win_trades"] += 1
            if pnl > self.data["max_pnl"]:
                self.data["max_pnl"] = pnl
        elif pnl < 0:
            self.data["loss_trades"] += 1
            if pnl < self.data["max_loss"]:
                self.data["max_loss"] = pnl

        # 最近 100 笔
        self.data["recent_trades"].append({
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "direction": direction,
            "pnl": round(pnl, 4),
            "fee": round(fee, 4),
            "reason": reason,
        })
        if len(self.data["recent_trades"]) > 100:
            self.data["recent_trades"] = self.data["recent_trades"][-100:]

        self.save()

    def daily_snapshot(self, equity: float):
        """每日记录一个权益快照"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        snapshots = self.data["daily_snapshots"]

        # 如果今天已有快照，更新它
        if snapshots and snapshots[-1]["date"] == today:
            snapshots[-1]["equity"] = equity
        else:
            snapshots.append({
                "date": today,
                "equity": equity,
                "pnl_to_date": self.data["total_pnl"],
            })

        # 保留最近 90 天
        if len(snapshots) > 90:
            self.data["daily_snapshots"] = snapshots[-90:]
        self.save()

    def get_today_pnl(self) -> float:
        """今日盈亏"""
        snapshots = self.data["daily_snapshots"]
        if len(snapshots) < 2:
            return self.data["total_pnl"]
        return self.data["total_pnl"] - snapshots[-2].get("pnl_to_date", 0)

    def summary(self) -> dict:
        win_rate = self.data["win_trades"] / self.data["total_trades"] if self.data["total_trades"] > 0 else 0
        return {
            "strategy": self.strategy_name,
            "total_trades": self.data["total_trades"],
            "win_rate": win_rate,
            "total_pnl": self.data["total_pnl"],
            "today_pnl": self.get_today_pnl(),
            "total_fee": self.data["total_fee"],
            "max_pnl": self.data["max_pnl"],
            "max_loss": self.data["max_loss"],
        }


def get_all_summaries() -> list[dict]:
    """读取所有策略的统计汇总（用于日报）"""
    if not os.path.exists(STATS_DIR):
        return []

    summaries = []
    for fname in sorted(os.listdir(STATS_DIR)):
        if fname.endswith(".json"):
            stats = StrategyStats(fname.replace(".json", ""))
            summaries.append(stats.summary())
    return summaries
