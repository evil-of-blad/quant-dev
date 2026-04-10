"""
网格交易策略

原理:
  在 [lower_price, upper_price] 区间内均分 N 个价格网格。
  价格每跨过一个网格往下 → 买入一份
  价格每跨过一个网格往上 → 卖出一份
  适合震荡行情，自动高抛低吸

特点:
  - 永续合约（用低杠杆 1~2x）
  - 中性起步：开仓时持有 N/2 份，向上向下都有空间
  - 触及边界则停止该方向操作
  - 启动时自动同步交易所持仓
  - 状态持久化到 grid_state.json，重启不丢失上次格点位置
"""
import asyncio
import json
import os
from datetime import datetime
from loguru import logger
from core.exchange import AsyncExchange
from core.notifier import TelegramNotifier
from core.allocation import CapitalAllocator


class GridTrader:

    def __init__(self, config: dict):
        self.config = config
        self.exchange = AsyncExchange(config)
        self.notifier = TelegramNotifier(config)
        self.allocator = CapitalAllocator(config)

        g = config.get("grid", {})
        self.symbol: str = g.get("symbol", "BTC/USDT:USDT")
        self.upper_price: float = g.get("upper_price", 80000.0)
        self.lower_price: float = g.get("lower_price", 65000.0)
        self.grid_count: int = g.get("grid_count", 20)
        # capital 由 allocator 在 start() 中动态计算
        self.capital: float = g.get("capital", 0.0)
        self.leverage: int = g.get("leverage", 2)
        self.check_interval: int = g.get("check_interval", 60)  # 60s 检查一次
        self.max_grids_per_tick: int = g.get("max_grids_per_tick", 3)  # 单次最多跨3格

        # 网格价格列表（从低到高）
        step = (self.upper_price - self.lower_price) / self.grid_count
        self.grid_prices = [self.lower_price + i * step for i in range(self.grid_count + 1)]
        self.grid_step = step

        # 每格仓位：总资金均分，每格对应一份合约张数
        # 中性持仓：起步持有 N/2 格的库存
        self.per_grid_value = self.capital / self.grid_count

        self._running = False
        self._last_grid_idx: int = None  # 上次价格在哪个格区间
        self._position_contracts: float = 0.0  # 当前持仓张数
        self._total_pnl: float = 0.0
        self._trade_count: int = 0

        # 状态文件
        self._state_file = os.path.join("logs", "grid_state.json")

    # ------------------------------------------------------------------
    async def start(self):
        await self.exchange.init()

        # 资金分配
        await self.allocator.init(self.exchange)
        allocated = self.allocator.get("grid")
        if allocated > 0:
            self.capital = allocated
            self.per_grid_value = self.capital / self.grid_count
            logger.info(f"[网格] 分配资金: {self.capital:.2f} USDT")

        # 设置杠杆 + 校验
        await self.exchange.set_margin_mode(self.symbol, "isolated", self.leverage)
        if not await self.exchange.set_leverage(self.symbol, self.leverage, "isolated"):
            logger.error(f"[网格] {self.symbol} 杠杆设置失败，无法启动")
            await self.notifier.notify_error(f"网格交易启动失败: {self.symbol} 杠杆设置异常")
            return

        # 加载市场信息
        self._markets = self.exchange.public.markets
        m = self._markets.get(self.symbol, {})
        self.contract_size = float(m.get("contractSize", 1) or 1)
        self.precision = m.get("precision", {}).get("amount", 1)

        # 计算每格对应张数
        # 名义价值 per_grid_value，对应 base coin 数量 = per_grid_value / 中间价
        mid_price = (self.upper_price + self.lower_price) / 2
        base_per_grid = self.per_grid_value * self.leverage / mid_price
        contracts_per_grid = base_per_grid / self.contract_size
        self.contracts_per_grid = self._round_amount(contracts_per_grid)

        if self.contracts_per_grid <= 0:
            logger.error(f"[网格] {self.symbol} 每格张数为0，资金太少或格数太多")
            await self.notifier.notify_error(
                f"网格交易: {self.symbol} 每格张数=0，请增加资金或减少格数"
            )
            return

        # 同步交易所真实持仓
        await self._reconcile_position()

        # 加载上次状态（last_grid_idx）
        self._load_state()

        # 一致性检查：如果 state 中的 idx 离当前价格太远，重置
        try:
            ticker = await self.exchange.fetch_ticker(self.symbol)
            curr_price = float(ticker.get("last", 0) or 0)
            if curr_price > 0 and self._last_grid_idx is not None:
                curr_idx = self._find_grid_index(curr_price)
                gap = abs(curr_idx - self._last_grid_idx)
                if gap > self.max_grids_per_tick:
                    logger.warning(
                        f"[网格] state 中 last_idx={self._last_grid_idx} 与当前 idx={curr_idx} "
                        f"相差 {gap} 格，超过补单上限，重置为当前格点"
                    )
                    self._last_grid_idx = curr_idx
                    self._save_state()
        except Exception as e:
            logger.warning(f"[网格] 启动一致性检查失败: {e}")

        logger.info(
            f"[网格] 启动 | {self.symbol} | "
            f"区间:[{self.lower_price:.2f}, {self.upper_price:.2f}] | "
            f"{self.grid_count}格 | 格距:{self.grid_step:.2f} | "
            f"每格:{self.contracts_per_grid}张 | 持仓:{self._position_contracts}张"
        )
        await self.notifier.send(
            f"📊 <b>网格交易启动</b>\n"
            f"标的: <code>{self.symbol}</code>\n"
            f"区间: <code>{self.lower_price:.2f} ~ {self.upper_price:.2f}</code>\n"
            f"格数: <code>{self.grid_count}</code> (格距 {self.grid_step:.2f})\n"
            f"每格: <code>{self.contracts_per_grid}</code> 张\n"
            f"杠杆: {self.leverage}x\n"
            f"当前持仓: <code>{self._position_contracts}</code> 张"
        )

        self._running = True
        await self._main_loop()

    async def stop(self):
        self._running = False
        self._save_state()
        await self.exchange.close()
        logger.info("[网格] 已停止")

    # ------------------------------------------------------------------
    async def _main_loop(self):
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[网格] 主循环异常: " + str(e), exc_info=True)
                await self.notifier.notify_error(f"网格交易异常: {e}")
            await asyncio.sleep(self.check_interval)

    async def _tick(self):
        ticker = await self.exchange.fetch_ticker(self.symbol)
        price = float(ticker.get("last", 0) or 0)
        if price <= 0:
            return

        # 区间外：跳过交易但记录
        if price < self.lower_price:
            logger.warning(f"[网格] 价格 {price:.2f} 跌破下限 {self.lower_price:.2f}，等待回归")
            return
        if price > self.upper_price:
            logger.warning(f"[网格] 价格 {price:.2f} 突破上限 {self.upper_price:.2f}，等待回归")
            return

        current_idx = self._find_grid_index(price)

        if self._last_grid_idx is None:
            self._last_grid_idx = current_idx
            logger.info(f"[网格] 初始化 当前价:{price:.4f} 格点:{current_idx}")
            self._save_state()
            return

        if current_idx == self._last_grid_idx:
            return

        # 跨格了
        diff = current_idx - self._last_grid_idx
        abs_diff = abs(diff)

        # ★ 安全保护：单次跨格不超过 max_grids_per_tick
        # 长时间停机或重启后，价格可能跨过很多格，不补做停机期间的交易，
        # 直接重置基准，避免一次性大额错单或与持仓数量不匹配
        if abs_diff > self.max_grids_per_tick:
            logger.warning(
                f"[网格] 跨格 {abs_diff} 超过单次上限 {self.max_grids_per_tick}，"
                f"重置基准跳过补单 (last={self._last_grid_idx} → curr={current_idx})"
            )
            await self.notifier.send(
                f"⚠️ <b>网格跨格异常</b>\n"
                f"标的: <code>{self.symbol}</code>\n"
                f"跨过 {abs_diff} 格 (上限 {self.max_grids_per_tick})\n"
                f"已重置基准，跳过补单（可能是重启或停机过久）"
            )
            self._last_grid_idx = current_idx
            self._save_state()
            return

        if diff < 0:
            await self._buy_grid(abs_diff, price)
        else:
            await self._sell_grid(diff, price)

        self._last_grid_idx = current_idx
        self._save_state()

    def _find_grid_index(self, price: float) -> int:
        """返回价格落在哪个格区间（0 ~ grid_count-1）"""
        if price <= self.lower_price:
            return 0
        if price >= self.upper_price:
            return self.grid_count - 1
        idx = int((price - self.lower_price) / self.grid_step)
        return min(idx, self.grid_count - 1)

    async def _buy_grid(self, grids: int, price: float):
        amount = self.contracts_per_grid * grids
        amount = self._round_amount(amount)
        if amount <= 0:
            return

        # 校验杠杆
        if not await self.exchange.ensure_leverage(self.symbol, self.leverage, "isolated"):
            logger.warning("[网格] 杠杆校验失败，跳过买入")
            return

        try:
            order = await self.exchange.create_market_order(self.symbol, "buy", amount)
            filled = order.get("average", price) or price
            self._position_contracts += amount
            self._trade_count += 1
            logger.success(
                f"[网格] 买入 {amount}张 @ {filled:.2f} | 持仓:{self._position_contracts}张 | "
                f"跨格:{grids}"
            )
            await self.notifier.send(
                f"🟢 <b>网格买入</b>\n"
                f"价格: <code>{filled:.2f}</code>\n"
                f"数量: <code>{amount}</code> 张 (跨{grids}格)\n"
                f"持仓: <code>{self._position_contracts}</code> 张"
            )
        except Exception as e:
            logger.error(f"[网格] 买入失败: {e}")
            await self.notifier.notify_error(f"网格买入失败: {e}")

    async def _sell_grid(self, grids: int, price: float):
        amount = self.contracts_per_grid * grids
        amount = self._round_amount(amount)

        # 不能卖出超过持仓
        if amount > self._position_contracts:
            amount = self._round_amount(self._position_contracts)
            if amount <= 0:
                logger.info("[网格] 持仓为0，跳过卖出")
                return

        if not await self.exchange.ensure_leverage(self.symbol, self.leverage, "isolated"):
            logger.warning("[网格] 杠杆校验失败，跳过卖出")
            return

        try:
            order = await self.exchange.create_market_order(self.symbol, "sell", amount)
            filled = order.get("average", price) or price
            self._position_contracts -= amount
            self._trade_count += 1
            logger.success(
                f"[网格] 卖出 {amount}张 @ {filled:.2f} | 持仓:{self._position_contracts}张 | "
                f"跨格:{grids}"
            )
            await self.notifier.send(
                f"🔴 <b>网格卖出</b>\n"
                f"价格: <code>{filled:.2f}</code>\n"
                f"数量: <code>{amount}</code> 张 (跨{grids}格)\n"
                f"持仓: <code>{self._position_contracts}</code> 张"
            )
        except Exception as e:
            logger.error(f"[网格] 卖出失败: {e}")
            await self.notifier.notify_error(f"网格卖出失败: {e}")

    # ------------------------------------------------------------------
    async def _reconcile_position(self):
        """启动时同步交易所合约持仓"""
        try:
            positions = await self.exchange.client.fetch_positions([self.symbol])
            for p in positions:
                if p.get("symbol") == self.symbol:
                    contracts = float(p.get("contracts", 0) or 0)
                    side = p.get("side")
                    if contracts > 0 and side == "long":
                        self._position_contracts = contracts
                        logger.info(f"[网格] 恢复持仓: {contracts}张 (long)")
                    elif contracts > 0 and side == "short":
                        logger.warning(
                            f"[网格] 检测到空单 {contracts}张，网格策略只做多，请手动平掉"
                        )
                        await self.notifier.notify_error(
                            f"网格交易: {self.symbol} 检测到空单持仓，请手动平掉"
                        )
                    break
        except Exception as e:
            logger.warning(f"[网格] 同步持仓失败: {e}")

    def _round_amount(self, amount: float) -> float:
        """按精度截断"""
        if isinstance(self.precision, int):
            factor = 10 ** self.precision
            return int(amount * factor) / factor
        else:
            return int(amount / self.precision) * self.precision

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
            with open(self._state_file, "w") as f:
                json.dump({
                    "last_grid_idx": self._last_grid_idx,
                    "position_contracts": self._position_contracts,
                    "trade_count": self._trade_count,
                    "saved_at": datetime.utcnow().isoformat(),
                }, f, indent=2)
        except Exception as e:
            logger.warning(f"[网格] 保存状态失败: {e}")

    def _load_state(self):
        if not os.path.exists(self._state_file):
            return
        try:
            with open(self._state_file) as f:
                state = json.load(f)
            self._last_grid_idx = state.get("last_grid_idx")
            self._trade_count = state.get("trade_count", 0)
            # 持仓数量以交易所为准（_reconcile 已设置），不从文件覆盖
            logger.info(
                f"[网格] 恢复状态: last_grid={self._last_grid_idx}, "
                f"trade_count={self._trade_count}"
            )
        except Exception as e:
            logger.warning(f"[网格] 加载状态失败: {e}")
