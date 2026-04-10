# 虚拟货币量化交易系统

基于 Python + CCXT 构建的永续合约量化交易框架，对接 OKX 交易所。
支持回测、参数优化、多策略实盘并行、Telegram 通知与远程指令。

---

## 目录结构

```
quant-dev/
├── config/
│   └── config.yaml              # 所有配置（API Key / 策略 / 风控 / 资金分配）
│
├── core/                        # 核心引擎
│   ├── exchange.py              # 交易所连接（公开行情 + 私有账户分离）
│   ├── data_feed.py             # 历史K线拉取、技术指标、本地缓存
│   ├── backtester.py            # 回测引擎（合约 + 杠杆 + 资金费率 + 强平）
│   ├── portfolio.py             # 仓位管理（多/空/杠杆/保证金/强平）
│   ├── risk.py                  # 风控（ATR动态止损 / 移动止盈 / 熔断 / 冷却重启）
│   ├── order.py                 # 订单数据结构
│   ├── allocation.py            # 多策略资金分配（按百分比隔离）
│   ├── notifier.py              # Telegram 通知
│   └── bot_commands.py          # Telegram 远程指令处理
│
├── strategies/                  # 策略模块
│   ├── base.py                  # 策略基类
│   ├── ma_crossover.py          # 双均线交叉（含趋势过滤）
│   ├── rsi_strategy.py          # RSI 超买超卖
│   ├── bollinger_bands.py       # 布林带（当前主策略）
│   ├── td_sequential.py         # 神奇九转
│   ├── combo.py                 # 多策略组合
│   ├── adaptive.py              # ADX 自适应
│   └── registry.py              # 策略注册表
│
├── arbitrage/                   # 套利与网格策略
│   ├── funding_arb.py           # 资金费率套利（现货+合约对冲）
│   └── grid_trader.py           # 网格交易（永续合约）
│
├── analysis/
│   ├── metrics.py               # 绩效指标
│   └── visualizer.py            # 回测四联图
│
├── live/
│   └── trader.py                # 布林带实盘引擎（含 Telegram bot）
│
├── scripts/                     # 后台启动 & 部署脚本
│   ├── start.sh                 # 布林带后台运行
│   ├── start_arb.sh             # 套利后台运行
│   ├── start_grid.sh            # 网格后台运行
│   ├── deploy.sh                # systemd 服务安装
│   ├── rebalance_arb.py         # 套利仓位失衡修复工具
│   └── quant-trader.service.template
│
├── data/                        # K线数据缓存（parquet）
├── logs/                        # 日志 + 回测图表 + 网格状态文件
├── backtest_runner.py           # 回测入口
├── optimize.py                  # 参数网格搜索（支持跨周期）
├── main.py                      # 布林带实盘入口
├── funding_arb_runner.py        # 套利实盘入口
├── grid_runner.py               # 网格实盘入口
├── Makefile                     # 快捷命令
├── DEPLOY.md                    # 服务器部署教程
└── requirements.txt
```

---

## 三套并行策略

| 策略 | 标的 | 杠杆 | 周期 | 入口 |
|------|------|------|------|------|
| **布林带** | BTC + XRP | 5x | 4h | `main.py` |
| **资金费率套利** | ETH + SOL + DOGE | 1x | 1h 检查 | `funding_arb_runner.py` |
| **网格交易** | LINK | 2x | 1min 检查 | `grid_runner.py` |

三个策略**完全独立运行**，通过 `core/allocation.py` 按百分比隔离资金，互不冲突。

---

## 资金分配

`config.yaml` 中按账户总额百分比分配：

```yaml
allocation:
  bollinger_pct: 0.40    # 布林带 40%
  funding_arb_pct: 0.30  # 套利 30%
  grid_pct: 0.20         # 网格 20%
  # 剩余 10% 缓冲
```

启动时各策略自动按比例计算自己的额度，**不会争用全局 USDT**。

---

## 快速开始

### 1. 安装依赖

```bash
make install
# 或
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### 2. 配置 API Key

```bash
nano config/config.yaml
```

填入：
```yaml
exchange:
  api_key: "..."
  api_secret: "..."
  passphrase: "..."
  sandbox: false   # true=模拟盘
```

### 3. 跑回测

```bash
make backtest-all                                       # 默认策略
make backtest STRATEGY=ma_crossover SYMBOL=BTC/USDT:USDT
make optimize                                           # 参数网格搜索
```

### 4. 启动实盘（三个策略独立启动）

```bash
# 方式 A：bash 脚本
bash scripts/start.sh         # 布林带
bash scripts/start_arb.sh     # 套利
bash scripts/start_grid.sh    # 网格

# 方式 B：Makefile
make start
make arb-start
make grid-start

# 方式 C：systemd（适合服务器）
make service-install
make service-start
```

详细的服务器部署见 [DEPLOY.md](DEPLOY.md)。

---

## Telegram 通知与指令

在 `config.yaml` 配置后，所有策略事件自动推送到手机：

```yaml
telegram:
  enabled: true
  bot_token: "你的BotFather Token"
  chat_id: "你的chat_id"
  bot_poll_interval: 15      # 指令轮询间隔（秒）
  status_interval: 6         # 定时状态播报（每N tick）
```

**支持的远程指令（在 Telegram 里发送）：**

| 指令 | 功能 |
|------|------|
| `/status` | 查看当前持仓和浮盈 |
| `/signal` | 查看 BTC/XRP 当前策略信号状态 |
| `/pnl` | 查看累计盈亏和最近交易 |
| `/balance` | 查看账户余额、资金分配、利用情况 |
| `/realloc` | 重新分配布林带资金（充值/提现后用） |
| `/help` | 显示指令帮助 |

**自动通知事件：**

- 🚀 启动 / 🔄 持仓恢复
- 🟢 开多 / 🔴 开空 / ✅ 平仓 / ❌ 止损 / 🎯 止盈
- ⚠️ 熔断触发 / 套利不平衡警告
- 💰 套利日报 / 网格跨格异常
- 🚨 异常报警

---

## 核心模块说明

### `core/exchange.py` — 交易所连接

- `public` 客户端：拉行情，无需 Key，不走 sandbox
- `client` 客户端：账户/下单，按 config 走 sandbox 或正式
- `set_leverage` / `set_margin_mode` 自动处理 OKX 单向持仓多种 posSide
- `ensure_leverage` 每次下单前校验杠杆，避免 OKX 后台修改导致失误

### `core/data_feed.py` — 数据与指标

含 SMA / EMA / MACD / RSI / Bollinger / ATR / ADX 等指标，自动 parquet 缓存。

### `core/backtester.py` — 回测引擎

合约回测完整模拟：
- 手续费 + 滑点 + 资金费率
- 强平检测（逐仓维持保证金率 0.4%）
- ATR 动态止损 + 移动止盈
- 最大回撤熔断 + 冷却重启
- 多空双向

### `core/risk.py` — 风险管理

- **ATR 动态止损**：`止损距离 = ATR × atr_stop_mult`，比固定百分比更适应波动
- **移动止盈**：盈利达激活阈值后追踪最有利价，回撤超阈值平仓
- **熔断 + 冷却**：最大回撤触发停止交易，冷却 N 根 K 线后重置峰值恢复

### `core/portfolio.py` — 仓位管理

支持多空双向，逐仓保证金，每个 symbol 同时只持一个方向。

### `core/allocation.py` — 资金分配

启动时一次性按百分比从账户总额分配给三个策略，每个策略只看自己的额度。

### `arbitrage/funding_arb.py` — 资金费率套利

- 费率高时买现货 + 空合约对冲（多空抵消，价格波动不影响）
- 每 8 小时收资金费率
- 费率回落时平掉两边
- 原子化下单：合约失败立即回滚现货，避免单边敞口
- 启动时同步 + 失衡检测 + 灰尘忽略

### `arbitrage/grid_trader.py` — 网格交易

- 在 [lower, upper] 区间均分 N 格
- 价格穿过格点自动买卖
- `max_grids_per_tick` 限制单次最多跨几格，防止重启或停机后大额错单
- `logs/grid_state.json` 持久化网格状态

### `live/trader.py` — 布林带实盘引擎

- 异步轮询，按 4h 周期检查 BTC + XRP 信号
- 每次开仓前 `ensure_leverage` 校验
- 启动时仓位同步 + 浮亏预警
- 集成 Telegram bot 命令处理

---

## 内置策略

| 策略 | 信号逻辑 | 适用场景 |
|------|---------|---------|
| `bollinger_bands` | 布林带 + RSI 双向反转 | **当前主策略**，震荡市 |
| `ma_crossover` | 双均线金叉死叉 + SMA200 趋势过滤 | 趋势行情 |
| `rsi` | RSI 超买超卖 | 短线反弹 |
| `td_sequential` | 神奇九转（连续9根反转） | 反转抄底/摸顶 |
| `combo` | 双均线 + 布林带组合 | 多重确认 |
| `adaptive` | ADX 判断趋势/震荡，动态选策略 | 自适应 |

### 编写自定义策略

```python
from strategies.base import BaseStrategy

class MyStrategy(BaseStrategy):
    def __init__(self, params):
        super().__init__(params)
        self.my_param = params.get("my_param", 10)

    def generate_signal(self, df, symbol):
        # 1=做多, -1=做空, 0=不动
        if df["rsi_14"].iloc[-1] < 30:
            return 1
        return 0
```

在 `strategies/registry.py` 注册后即可通过 `--strategy my_strategy` 调用。

---

## 配置文件全貌

```yaml
exchange:
  name: okx
  api_key: ""
  api_secret: ""
  passphrase: ""
  sandbox: false
  market_type: swap

trading:
  symbols:
    - BTC/USDT:USDT
    - XRP/USDT:USDT
  timeframe: 4h
  leverage: 5
  margin_mode: isolated

risk:
  max_position_pct: 0.2          # 单笔最大保证金占比
  stop_loss_pct: 0.03            # 备用固定止损（ATR 关闭时用）
  take_profit_pct: 0.06
  max_drawdown_pct: 0.15         # 最大回撤熔断阈值
  risk_per_trade_pct: 0.01       # 单笔风险占总资金
  cooldown_bars: 100             # 熔断后冷却K线数
  use_atr_stop: true             # 启用 ATR 动态止损
  atr_stop_mult: 2.5             # 止损 = ATR × 2.5
  use_trailing_stop: false       # 移动止盈（布林带不适用）

allocation:
  bollinger_pct: 0.40
  funding_arb_pct: 0.30
  grid_pct: 0.20

backtest:
  initial_capital: 10000.0
  fee_rate: 0.0005               # 合约 Taker 0.05%
  slippage_pct: 0.0002
  funding_rate: 0.0001

strategy:
  name: bollinger_bands
  params:
    bb_period: 30
    bb_std: 2.0
    rsi_oversold: 35

grid:
  symbol: LINK/USDT:USDT
  upper_price: 9.85
  lower_price: 8.05
  grid_count: 20
  capital: 400.0                 # 会被 allocation 动态覆盖
  leverage: 2
  max_grids_per_tick: 3

funding_arb:
  symbols:
    - ETH/USDT:USDT
    - SOL/USDT:USDT
    - DOGE/USDT:USDT
  capital: 600.0                 # 会被 allocation 动态覆盖
  per_symbol_pct: 0.25
  entry_rate: 0.0001             # 费率 ≥ 0.01% 开仓
  exit_rate: 0.00005
  leverage: 1

telegram:
  enabled: true
  bot_token: ""
  chat_id: ""
  bot_poll_interval: 15
  status_interval: 6
```

---

## 常用命令速查

| 操作 | 命令 |
|------|------|
| 安装依赖 | `make install` |
| 跑回测 | `make backtest-all` |
| 参数优化 | `make optimize` |
| 启动布林带 | `make start` |
| 启动套利 | `make arb-start` |
| 启动网格 | `make grid-start` |
| 停止布林带 | `make stop` |
| 停止套利 | `make arb-stop` |
| 停止网格 | `make grid-stop` |
| 看布林带日志 | `make log` |
| 看套利日志 | `make arb-log` |
| 看网格日志 | `make grid-log` |
| 修复套利失衡 | `venv/bin/python scripts/rebalance_arb.py --fix` |

---

## 回测绩效参考

### 布林带（BTC, 5x, 跨周期参数）

| 时间段 | 收益率 | 夏普 | 最大回撤 |
|--------|--------|------|----------|
| 2023 | +11.93% | - | - |
| 2024 | +11.12% | - | - |
| 2025 | +42.69% | - | - |
| **三年合计** | **+59.40%** | **1.49** | -23.42% |

### 网格（LINK, 2x, 24-25）

| 时间段 | 收益率 | 交易次数 |
|--------|--------|----------|
| 2024 | +55.53% | - |
| 2025 | +12.54% | - |
| **24-25 合计** | **+60.21%** | 706 |

---

## 注意事项

1. **先模拟盘，再实盘**：`sandbox: true` 跑稳定后再切 `false`
2. **首次启动前**：在 OKX 网页对每个标的设置「逐仓 + 对应杠杆」，程序会校验
3. **充值/提现后**：发 `/realloc` 重新分配布林带，套利和网格需要重启
4. **现货与合约共用账户**：套利策略要求 OKX 账户模式支持现货 + 合约同时持仓
5. **网格区间过期**：定期检查 `grid.upper_price` / `lower_price` 是否还覆盖当前价
6. **历史不代表未来**：回测仅供参考，市场状态会变化
7. **杠杆风险**：合约放大收益也放大亏损，从低杠杆开始
