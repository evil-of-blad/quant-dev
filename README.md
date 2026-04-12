# 虚拟货币量化交易系统

基于 Python + CCXT 的永续合约量化交易系统，对接 OKX 交易所。
支持回测、参数优化、实盘自动交易、Telegram 远程控制、市场告警。

---

## 当前生产配置

| 项目 | 配置 |
|------|------|
| 策略 | 双均线 MA15/50 + SMA200 趋势过滤 |
| 标的 | BTC + LINK + BCH（等权重） |
| 杠杆 | **3-5x 动态**（ADX 斜率驱动） |
| 保证金 | 逐仓 (isolated) |
| 资金分配 | 策略 85% + 缓冲 15% |
| 时间周期 | 4h K 线 |
| 止损检查 | 每 30 分钟 |
| 信号检查 | 每 4h K 线收盘 |

### 动态杠杆逻辑

ADX 3 根 K 线斜率 > 3.0 且 ADX > 15 时切 5x（趋势刚启动），否则 3x。
5 年回测 19 个币种验证，样本外胜率 64%。

### 5 年回测结果（2021-2025）

| 指标 | 固定 3x | ADX 动态 3-5x |
|------|---------|--------------|
| 收益 | +80.2% | **+121.6%** |
| Sharpe | 1.16 | **1.27** |
| MaxDD | -18.7% | **-18.0%** |
| 收益/回撤比 | 4.30 | **6.77** |

详细回测数据见 [BACKTEST_RESULTS.md](BACKTEST_RESULTS.md)。

---

## 目录结构

```
quant-dev/
├── config/
│   └── config.yaml              # 配置（API Key / 策略 / 风控 / 资金分配）
│
├── core/                        # 核心引擎
│   ├── exchange.py              # OKX 连接（公开行情 + 私有账户分离）
│   ├── data_feed.py             # K 线拉取 + 技术指标 + parquet 缓存
│   ├── backtester.py            # 回测引擎（合约 + 杠杆 + 费率 + 强平）
│   ├── portfolio.py             # 仓位管理（多空 / 杠杆 / 逐仓保证金）
│   ├── risk.py                  # 风控（ATR 止损 / 移动止盈 / 熔断 + 冷却）
│   ├── order.py                 # 订单数据结构
│   ├── allocation.py            # 资金分配（按百分比隔离）
│   ├── stats.py                 # 持久化策略统计（logs/stats/*.json）
│   ├── notifier.py              # Telegram 通知
│   └── bot_commands.py          # Telegram 指令（/status /signal /alert 等）
│
├── strategies/                  # 策略
│   ├── base.py                  # 基类 ABC
│   ├── ma_crossover.py          # ★ 当前主策略（15/50 + SMA200 过滤）
│   ├── bollinger_bands.py       # 布林带（已废弃，-48%）
│   ├── rsi_strategy.py          # RSI
│   ├── td_sequential.py         # 神奇九转（已废弃）
│   ├── adaptive.py              # 自适应（已废弃）
│   ├── smart_adaptive.py        # 智能自适应（已废弃，-32%）
│   ├── combo.py                 # 组合（已废弃）
│   ├── regime_detector.py       # 市场状态识别（4 态）
│   └── registry.py              # name → class 注册表
│
├── arbitrage/                   # 套利（均已停用）
│   ├── funding_arb.py           # 资金费率套利（停用：手续费不成立）
│   └── grid_trader.py           # 网格（停用：5y 仅 +2.6%）
│
├── alert/                       # 市场告警引擎（独立进程，不交易）
│   ├── alert_engine.py          # 9 指标加权评分 + 分级推送
│   ├── indicators.py            # 9 个评分函数（0-100 极端度）
│   ├── backtest.py              # 告警系统历史回测
│   └── data_sources/            # 数据源
│       ├── fear_greed.py        # alternative.me 恐贪指数
│       ├── coingecko.py         # 全球市值 + BTC dominance
│       ├── defillama.py         # 稳定币市值变化
│       ├── okx_metrics.py       # OKX K 线 + 费率
│       ├── okx_rubik.py         # OKX OI + 多空比
│       └── hyperliquid.py       # HL 溢价 + 费率
│
├── analysis/
│   ├── metrics.py               # 绩效指标计算
│   └── visualizer.py            # 回测图表
│
├── live/
│   └── trader.py                # 实盘引擎（双档轮询 + 动态杠杆 + Telegram bot）
│
├── scripts/
│   ├── start.sh                 # 主策略后台启动
│   ├── start_alert.sh           # 告警引擎后台启动
│   ├── deploy.sh                # systemd 服务安装
│   └── quant-trader.service.template
│
├── main.py                      # 主策略入口
├── alert_runner.py              # 告警引擎入口
├── backtest_runner.py           # 回测入口
├── optimize.py                  # 参数网格搜索
├── BACKTEST_RESULTS.md          # 回测结果档案（所有策略历史）
├── DEPLOY.md                    # 服务器部署教程
└── requirements.txt
```

---

## 快速开始

### 1. 安装依赖

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### 2. 配置

```bash
cp config/config.yaml config/config.yaml.bak
nano config/config.yaml
```

必填项：
```yaml
exchange:
  api_key: "你的 OKX API Key"
  api_secret: "你的 Secret"
  passphrase: "你的 Passphrase"
  sandbox: false      # true=模拟盘

telegram:
  enabled: true
  bot_token: "BotFather Token"
  chat_id: "你的 chat_id"
```

### 3. 回测

```bash
venv/bin/python backtest_runner.py
venv/bin/python backtest_runner.py --strategy ma_crossover --symbol BTC/USDT:USDT --start 2021-01-01 --end 2025-12-31
```

### 4. 启动实盘

```bash
# 主策略
bash scripts/start.sh

# 告警引擎（独立进程，可选）
bash scripts/start_alert.sh

# 或用 systemd（服务器推荐）
sudo bash scripts/deploy.sh
sudo systemctl start quant-trader
```

详细部署见 [DEPLOY.md](DEPLOY.md)。

---

## Telegram 指令

| 指令 | 功能 |
|------|------|
| `/status` | 持仓 + 权益 + 浮盈 |
| `/signal` | MA 交叉状态 + SMA200 偏离 |
| `/alert` | 实时 9 项市场指标分析 |
| `/pnl` | 盈亏统计 + 最近交易 |
| `/balance` | 余额 + 资金分配 |
| `/realloc` | 重新分配资金（充提后使用） |
| `/report` | 策略统计日报 |

自动通知：
- 开仓/平仓/止损/止盈（含杠杆倍数）
- 熔断触发
- 启动补仓（检测到错过的信号）
- 异常报警
- 每日 09:00 / 17:00 定时播报（北京时间）

---

## 核心机制

### 策略信号

MA15/50 交叉 + SMA200 趋势过滤：
- 金叉 + 价格在 SMA200 上方 → 做多
- 死叉 + 价格在 SMA200 下方 → 做空
- 其他情况 → 不开仓

信号只在交叉发生的 K 线触发。启动时 `_catchup_missed_signals` 检查是否有遗漏信号并自动补仓。

### 动态杠杆

开仓时检查 ADX 趋势强度：
- ADX 3 根 K 线变化 > 3.0 且 ADX > 15 → **5x**（趋势刚启动）
- 否则 → **3x**

### 双档轮询

| 检查项 | 频率 | 说明 |
|--------|------|------|
| 止损/止盈 | 30 分钟 | 拉 20 根 K 线算 ATR，轻量 |
| 策略信号 | 4h 收盘 | UTC 0/4/8/12/16/20，避免 forming candle 假信号 |

### 风控

- **ATR 动态止损**：止损距离 = ATR x 2.5
- **移动止盈**：盈利达阈值后追踪最有利价，回撤超阈值平仓
- **熔断**：账户回撤 > 15% → 暂停交易，冷却 100 根 K 线后恢复
- **OKX 合约张数转换**：自动处理 base coin ↔ 合约张数

### 告警引擎（9 指标）

独立进程，不交易，只推送 Telegram 提醒：

| 指标 | 数据源 | 权重 |
|------|--------|------|
| 恐贪指数 | alternative.me | 1.5 |
| MA200 偏离 | OKX | 1.5 |
| 资金费率 | OKX | 1.0 |
| BTC dominance | CoinGecko | 0.8 |
| 稳定币市值变化 | DefiLlama | 1.2 |
| OI 变化 | OKX Rubik | 1.0 |
| 多空比 | OKX Rubik | 1.0 |
| HL 溢价 | Hyperliquid | 1.2 |
| HL 费率 | Hyperliquid | 1.0 |

5 年回测准确率 55%，平均提前 38.7 天预警。

---

## 已废弃策略及原因

| 策略 | 5y 收益 | 废弃原因 |
|------|---------|---------|
| 布林带 | -48% | 2021-22 熊市灾难 |
| 智能自适应 | -32% | 过度设计，不如简单 MA |
| 神奇九转 | -13%~-34% | BTC 趋势太强，反转信号被碾压 |
| 网格 (UNI) | +2.6% | 5y 收益接近 0，2022 单年 -42.5% |
| 资金费率套利 | 亏损 | Regular tier 手续费结构性不成立 |
| 马丁格尔 | +7.78% | 风险收益不对称，评估后未部署 |

详见 [BACKTEST_RESULTS.md](BACKTEST_RESULTS.md)。

---

## 注意事项

1. **先模拟盘再实盘**：`sandbox: true` 跑稳定后再切 `false`
2. **首次启动前**：在 OKX 网页对每个标的设置「单向持仓 + 逐仓」
3. **充提后**：Telegram 发 `/realloc` 重新分配资金
4. **合约张数**：OKX swap 的 amount 是合约张数不是 base 数量（BTC 1 张 = 0.01 BTC）
5. **杠杆设置**：OKX 杠杆按 margin_mode x posSide 维度存储，代码会自动处理
6. **历史不代表未来**：回测仅供参考，实盘效果可能偏离
7. **config.yaml 含敏感信息**：不要提交真实 API Key 到 git
