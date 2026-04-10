"""
资金分配模块 — 多策略隔离

原理:
  启动时拉取账户总 USDT，按 config 里的 allocation 百分比分配给每个策略。
  每个策略只看自己的额度，不再争用全局 USDT 余额。

  分配在策略生命周期内固定，账户有充值/提现需要重启策略。

config 示例:
  allocation:
    bollinger_pct: 0.40    # 布林带占 40%
    funding_arb_pct: 0.30  # 套利占 30%
    grid_pct: 0.20         # 网格占 20%
    # 剩余 10% 作为缓冲，避免保证金不足
"""
from loguru import logger


class CapitalAllocator:

    def __init__(self, config: dict):
        self.config = config
        self._total_balance: float = 0.0
        self._allocations: dict[str, float] = {}

    async def init(self, exchange) -> dict[str, float]:
        """启动时拉取账户余额并按比例分配"""
        try:
            balance = await exchange.fetch_balance()
            self._total_balance = balance.get("USDT", {}).get("total", 0) or 0
        except Exception as e:
            logger.warning(f"[资金分配] 拉取余额失败，使用 0: {e}")
            self._total_balance = 0

        alloc_cfg = self.config.get("allocation", {})
        strategies = ["bollinger", "funding_arb", "grid"]

        total_pct = 0.0
        for s in strategies:
            pct = alloc_cfg.get(f"{s}_pct", 0.0)
            self._allocations[s] = self._total_balance * pct
            total_pct += pct

        reserve_pct = max(0, 1.0 - total_pct)

        logger.info("=" * 50)
        logger.info(f"资金分配 | 账户总额: {self._total_balance:.2f} USDT")
        logger.info("-" * 50)
        for s, amt in self._allocations.items():
            pct = alloc_cfg.get(f"{s}_pct", 0.0)
            logger.info(f"  {s:<14}: {amt:>10.2f} USDT ({pct:.0%})")
        logger.info(f"  {'reserve':<14}: {self._total_balance * reserve_pct:>10.2f} USDT ({reserve_pct:.0%})")
        logger.info("=" * 50)

        if total_pct > 1.0:
            logger.warning(f"⚠️ allocation 总和 {total_pct:.0%} 超过 100%，存在资金冲突风险！")

        return self._allocations

    def get(self, strategy: str) -> float:
        """获取策略分配的资金"""
        amount = self._allocations.get(strategy, 0.0)
        if amount <= 0:
            logger.warning(f"[资金分配] {strategy} 未分配资金，请检查 config.allocation")
        return amount

    @property
    def total_balance(self) -> float:
        return self._total_balance
