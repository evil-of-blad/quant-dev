"""
网格交易策略（限价单挂单版）

原理:
  在 [lower_price, upper_price] 区间内均分 N 个价格网格。
  启动时:
    1. 取消该 symbol 所有旧挂单
    2. 同步交易所真实持仓
    3. 必要时市价建立中性持仓（N/2 份库存）
    4. 在每个格点挂限价单（当前价以下挂买单，以上挂卖单）

  运行时:
    每隔 N 秒查询挂单状态:
      - 已成交的 buy 单 → 在上一格挂 sell 单（锁定利润）
      - 已成交的 sell 单 → 在下一格挂 buy 单（继续低买）

优势:
  - 限价单 Maker 费率（OKX 0.02%）vs 市价单 Taker（0.05%）
  - 不会因为滑点亏损
  - 程序短暂离线挂单依然生效
"""
import asyncio
import json
import os
from datetime import datetime
from loguru import logger
from core.exchange import AsyncExchange
from core.notifier import TelegramNotifier
from core.allocation import CapitalAllocator
from core.stats import StrategyStats


class GridTrader:

    def __init__(self, config: dict):
        self.config = config
        self.exchange = AsyncExchange(config)
        self.notifier = TelegramNotifier(config)
        self.allocator = CapitalAllocator(config)
        self.stats = StrategyStats("grid")

        g = config.get("grid", {})
        self.symbol: str = g.get("symbol", "BTC/USDT:USDT")
        self.upper_price: float = g.get("upper_price", 80000.0)
        self.lower_price: float = g.get("lower_price", 65000.0)
        self.grid_count: int = g.get("grid_count", 20)
        self.capital: float = g.get("capital", 0.0)
        self.leverage: int = g.get("leverage", 2)
        self.check_interval: int = g.get("check_interval", 30)
        # 边界保护
        self.boundary_breakout_bars: int = g.get("boundary_breakout_bars", 12)  # 价格连续N次检查在区间外
        self.max_loss_pct: float = g.get("max_loss_pct", 0.20)  # 最大亏损20%全平止损
        # 计数器
        self._out_of_range_count: int = 0
        self._initial_capital: float = 0.0  # 启动时的资金，用于计算亏损

        # 网格价格（从低到高）
        step = (self.upper_price - self.lower_price) / self.grid_count
        self.grid_prices = [self.lower_price + i * step for i in range(self.grid_count + 1)]
        self.grid_step = step
        self.per_grid_value = self.capital / self.grid_count

        self._running = False
        self._position_contracts: float = 0.0
        # price (rounded) -> {"id": str, "side": "buy"/"sell"}
        self._open_orders: dict[float, dict] = {}
        self._trade_count: int = 0

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
        self._initial_capital = self.capital

        # 杠杆 + 保证金模式
        await self.exchange.set_margin_mode(self.symbol, "isolated", self.leverage)
        if not await self.exchange.set_leverage(self.symbol, self.leverage, "isolated"):
            logger.error(f"[网格] {self.symbol} 杠杆设置失败，无法启动")
            await self.notifier.notify_error(f"网格交易启动失败: {self.symbol} 杠杆异常")
            return

        # 加载市场信息
        self._markets = self.exchange.public.markets
        m = self._markets.get(self.symbol, {})
        self.contract_size = float(m.get("contractSize", 1) or 1)
        self.amount_precision = m.get("precision", {}).get("amount", 1)
        self.price_precision = m.get("precision", {}).get("price", 0.01)

        # 计算每格张数
        mid_price = (self.upper_price + self.lower_price) / 2
        base_per_grid = self.per_grid_value * self.leverage / mid_price
        self.contracts_per_grid = self._round_amount(base_per_grid / self.contract_size)

        if self.contracts_per_grid <= 0:
            logger.error(f"[网格] 每格张数为0，资金太少或格数太多")
            await self.notifier.notify_error(f"网格交易: 每格张数=0")
            return

        # 同步真实持仓
        await self._reconcile_position()

        # 取消所有旧挂单（防止状态污染）
        await self._cancel_all_orders()

        # 建立初始中性库存（N/2 张）
        await self._setup_initial_position()

        # 挂初始网格
        await self._place_initial_grid()

        logger.info(
            f"[网格] 启动 | {self.symbol} | "
            f"区间:[{self.lower_price}, {self.upper_price}] | "
            f"{self.grid_count}格 | 每格:{self.contracts_per_grid}张 | "
            f"持仓:{self._position_contracts}张 | 挂单:{len(self._open_orders)}个"
        )
        await self.notifier.send(
            f"📊 <b>网格交易启动 (限价单)</b>\n"
            f"标的: <code>{self.symbol}</code>\n"
            f"区间: <code>{self.lower_price} ~ {self.upper_price}</code>\n"
            f"格数: <code>{self.grid_count}</code> (格距 {self.grid_step:.4f})\n"
            f"每格: <code>{self.contracts_per_grid}</code> 张\n"
            f"杠杆: {self.leverage}x | Maker费率\n"
            f"持仓: <code>{self._position_contracts}</code> 张\n"
            f"挂单: <code>{len(self._open_orders)}</code> 个"
        )

        self._running = True
        await self._main_loop()

    async def stop(self):
        self._running = False
        # 退出时不取消挂单（让它们留在 OKX 继续生效）
        await self.exchange.close()
        logger.info("[网格] 已停止（挂单保留）")

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
        """
        每个 tick 做三件事:
        1. 边界保护：检查价格是否长期超出区间
        2. 全局止损：检查累计亏损是否触发熔断
        3. 处理已成交挂单
        """
        # 每日快照
        self.stats.daily_snapshot(self._initial_capital + self.stats.data["total_pnl"])

        # 1. 边界保护
        try:
            ticker = await self.exchange.fetch_ticker(self.symbol)
            curr_price = float(ticker.get("last", 0) or 0)

            if curr_price > 0:
                if curr_price < self.lower_price or curr_price > self.upper_price:
                    self._out_of_range_count += 1
                    logger.warning(
                        f"[网格] 价格 {curr_price:.4f} 超出区间 "
                        f"[{self.lower_price}, {self.upper_price}] "
                        f"({self._out_of_range_count}/{self.boundary_breakout_bars})"
                    )
                    if self._out_of_range_count >= self.boundary_breakout_bars:
                        await self._handle_boundary_breakout(curr_price)
                        return
                else:
                    if self._out_of_range_count > 0:
                        logger.info(f"[网格] 价格回归区间，重置突破计数")
                    self._out_of_range_count = 0
        except Exception as e:
            logger.warning(f"[网格] 边界检查失败: {e}")

        # 2. 全局止损
        try:
            await self._check_max_loss(curr_price)
        except Exception as e:
            logger.warning(f"[网格] 止损检查失败: {e}")

        # 3. 处理已成交挂单
        try:
            current_orders = await self.exchange.client.fetch_open_orders(self.symbol)
        except Exception as e:
            logger.warning(f"[网格] 拉取挂单失败: {e}")
            return

        current_ids = {o["id"] for o in current_orders}
        filled_orders = []
        for price, info in list(self._open_orders.items()):
            if info["id"] not in current_ids:
                filled_orders.append((price, info))
                del self._open_orders[price]

        for price, info in filled_orders:
            await self._handle_filled_order(price, info)

    async def _handle_boundary_breakout(self, curr_price: float):
        """
        价格连续多次超出区间 → 自动重新调整网格
        以当前价为中心，重新计算 ±10% 区间
        """
        old_lower = self.lower_price
        old_upper = self.upper_price

        # 新区间：当前价 ±10%
        new_lower = curr_price * 0.90
        new_upper = curr_price * 1.10
        new_step = (new_upper - new_lower) / self.grid_count

        logger.warning(
            f"[网格] 触发边界自动调整 | "
            f"旧区间[{old_lower:.4f}, {old_upper:.4f}] → "
            f"新区间[{new_lower:.4f}, {new_upper:.4f}]"
        )

        await self.notifier.send(
            f"⚠️ <b>网格自动调整区间</b>\n"
            f"标的: <code>{self.symbol}</code>\n"
            f"当前价: <code>{curr_price:.4f}</code>\n"
            f"旧区间: <code>{old_lower:.4f} ~ {old_upper:.4f}</code>\n"
            f"新区间: <code>{new_lower:.4f} ~ {new_upper:.4f}</code>\n"
            f"取消所有旧挂单并重新挂单"
        )

        # 取消所有旧挂单
        await self._cancel_all_orders()
        self._open_orders.clear()

        # 更新区间
        self.lower_price = new_lower
        self.upper_price = new_upper
        self.grid_step = new_step
        self.grid_prices = [new_lower + i * new_step for i in range(self.grid_count + 1)]
        self._out_of_range_count = 0

        # 重新挂网格
        await self._place_initial_grid()

    async def _check_max_loss(self, curr_price: float):
        """
        计算当前总价值 = 现金 + 持仓市值 - 持仓成本（用 OKX 持仓接口的 unrealized PnL）
        亏损 > max_loss_pct 则全部平仓
        """
        try:
            positions = await self.exchange.client.fetch_positions([self.symbol])
            for p in positions:
                if p.get("symbol") == self.symbol:
                    upnl = float(p.get("unrealizedPnl", 0) or 0)
                    initial_margin = float(p.get("initialMargin", 0) or 0)
                    if initial_margin > 0:
                        loss_pct = upnl / self._initial_capital
                        if loss_pct < -self.max_loss_pct:
                            logger.error(
                                f"[网格] 触发全局止损! 浮亏 {upnl:.2f} USDT "
                                f"({loss_pct:.2%}) > 阈值 {self.max_loss_pct:.0%}"
                            )
                            await self.notifier.send(
                                f"🛑 <b>网格全局止损</b>\n"
                                f"标的: <code>{self.symbol}</code>\n"
                                f"浮亏: <code>{upnl:.2f} USDT</code> ({loss_pct:.2%})\n"
                                f"取消所有挂单并平仓"
                            )
                            await self._cancel_all_orders()
                            self._open_orders.clear()
                            # 平掉所有持仓
                            contracts = float(p.get("contracts", 0) or 0)
                            if contracts > 0:
                                side = "sell" if p.get("side") == "long" else "buy"
                                await self.exchange.create_market_order(self.symbol, side, contracts)
                            self._running = False  # 停止运行
                            return
                    break
        except Exception as e:
            logger.debug(f"[网格] 检查止损失败: {e}")

    async def _handle_filled_order(self, price: float, info: dict):
        """处理一笔成交，在反向方向挂新单"""
        try:
            order_detail = await self.exchange.client.fetch_order(info["id"], self.symbol)
            status = order_detail.get("status")

            if status != "closed":
                # 被取消了，不补单
                logger.info(f"[网格] 订单 {info['id']} 状态={status}，不补单")
                return

            filled_amt = float(order_detail.get("filled", 0) or 0)
            avg_price = float(order_detail.get("average", price) or price)
            side = info["side"]

            if filled_amt <= 0:
                return

            # 更新持仓
            if side == "buy":
                self._position_contracts += filled_amt
                # 在上一格挂 sell 单
                next_price = self._round_price(price + self.grid_step)
                next_side = "sell"
            else:
                self._position_contracts -= filled_amt
                # 在下一格挂 buy 单
                next_price = self._round_price(price - self.grid_step)
                next_side = "buy"

            self._trade_count += 1
            # 记录到 stats（卖出时计算 PnL，买入只记数）
            grid_pnl = 0.0
            if side == "sell":
                # 利润 = 一格价差 × 数量 × contract_size - 手续费
                grid_pnl = self.grid_step * filled_amt * self.contract_size - avg_price * filled_amt * self.contract_size * 0.0004
            self.stats.record_trade(self.symbol, side, grid_pnl, reason=f"网格成交@{avg_price:.4f}")

            logger.success(
                f"[网格] {side} 成交 {filled_amt}张 @ {avg_price:.4f} | "
                f"持仓:{self._position_contracts} | 在 {next_price} 挂 {next_side}"
            )

            await self.notifier.send(
                f"{'🟢' if side=='buy' else '🔴'} <b>网格成交</b>\n"
                f"方向: {side}\n"
                f"价格: <code>{avg_price:.4f}</code>\n"
                f"数量: <code>{filled_amt}</code> 张\n"
                f"持仓: <code>{self._position_contracts}</code> 张\n"
                f"补单: {next_side} @ <code>{next_price}</code>"
            )

            # 在反向格点挂新单
            if self.lower_price <= next_price <= self.upper_price:
                await self._place_order(next_price, next_side)
            else:
                logger.info(f"[网格] {next_price} 超出区间，不补单")

        except Exception as e:
            logger.error(f"[网格] 处理成交失败 {info['id']}: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # 挂单 / 取消
    # ------------------------------------------------------------------
    async def _cancel_all_orders(self):
        """取消该 symbol 所有挂单"""
        try:
            orders = await self.exchange.client.fetch_open_orders(self.symbol)
            if not orders:
                logger.info("[网格] 无旧挂单")
                return

            logger.info(f"[网格] 取消 {len(orders)} 个旧挂单...")
            for o in orders:
                try:
                    await self.exchange.client.cancel_order(o["id"], self.symbol)
                    logger.debug(f"[网格] 取消 {o['id']} {o['side']} @ {o['price']}")
                except Exception as e:
                    logger.warning(f"[网格] 取消 {o['id']} 失败: {e}")
        except Exception as e:
            logger.warning(f"[网格] 拉取旧挂单失败: {e}")

    async def _setup_initial_position(self):
        """
        建立中性库存：网格策略需要先持有 N/2 份合约才能往上挂卖单。
        如果当前持仓不足，市价补足；超过则不动。
        """
        target = self.contracts_per_grid * (self.grid_count // 2)
        diff = target - self._position_contracts
        diff = self._round_amount(diff)

        if diff < self.contracts_per_grid * 0.5:
            logger.info(
                f"[网格] 当前持仓 {self._position_contracts} 张，"
                f"目标 {target} 张，差额可忽略，跳过初始建仓"
            )
            return

        try:
            logger.info(f"[网格] 市价建立初始库存 {diff} 张...")
            order = await self.exchange.create_market_order(self.symbol, "buy", diff)
            filled = order.get("filled", diff) or diff
            self._position_contracts += filled
            logger.success(
                f"[网格] 初始库存建立完成: {filled} 张，当前持仓 {self._position_contracts}"
            )
        except Exception as e:
            logger.error(f"[网格] 初始建仓失败: {e}")
            await self.notifier.notify_error(f"网格初始建仓失败: {e}")

    async def _place_initial_grid(self):
        """
        在所有格点挂限价单:
          - 当前价以下：buy
          - 当前价以上：sell（需要持仓支撑）
          - 离当前价最近的一格不挂（避免立即成交）
        """
        try:
            ticker = await self.exchange.fetch_ticker(self.symbol)
            curr_price = float(ticker["last"])
        except Exception as e:
            logger.error(f"[网格] 获取当前价失败: {e}")
            return

        # 离当前价 < 1/2 格距的不挂
        skip_distance = self.grid_step / 2

        for price in self.grid_prices:
            if abs(price - curr_price) < skip_distance:
                continue

            if price < curr_price:
                # 挂 buy 单
                await self._place_order(self._round_price(price), "buy")
            else:
                # 挂 sell 单（需要有持仓）
                if self._position_contracts >= self.contracts_per_grid:
                    await self._place_order(self._round_price(price), "sell")

    async def _place_order(self, price: float, side: str):
        """挂一个限价单"""
        # 不重复挂相同价格
        if price in self._open_orders:
            logger.debug(f"[网格] {price} 已有挂单，跳过")
            return

        try:
            order = await self.exchange.client.create_limit_order(
                self.symbol, side, self.contracts_per_grid, price,
                params={"tdMode": "isolated"}
            )
            self._open_orders[price] = {
                "id": order["id"],
                "side": side,
            }
            logger.info(f"[网格] 挂{side} {self.contracts_per_grid}张 @ {price}")
        except Exception as e:
            logger.error(f"[网格] 挂单失败 {side} @ {price}: {e}")

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
                            f"[网格] 检测到空单 {contracts}张，网格只做多，请手动处理"
                        )
                        await self.notifier.notify_error(
                            f"网格交易: {self.symbol} 检测到空单，请手动平掉"
                        )
                    break
        except Exception as e:
            logger.warning(f"[网格] 同步持仓失败: {e}")

    def _round_amount(self, amount: float) -> float:
        if isinstance(self.amount_precision, int):
            factor = 10 ** self.amount_precision
            return int(amount * factor) / factor
        else:
            return round(int(amount / self.amount_precision) * self.amount_precision, 8)

    def _round_price(self, price: float) -> float:
        if isinstance(self.price_precision, int):
            factor = 10 ** self.price_precision
            return int(price * factor) / factor
        else:
            return round(int(price / self.price_precision) * self.price_precision, 8)
