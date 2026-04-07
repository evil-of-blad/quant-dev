# 虚拟货币量化交易系统

基于 Python + CCXT 构建的永续合约量化交易框架，对接 OKX 交易所，支持策略回测与模拟盘/实盘自动交易。

---

## 目录结构

```
quant-dev/
├── config/
│   └── config.yaml          # 所有配置（API Key、策略参数、风控参数）
├── core/                    # 核心引擎
│   ├── exchange.py          # 交易所连接（行情走公开接口，交易走账户接口）
│   ├── data_feed.py         # 历史K线拉取、技术指标计算、本地缓存
│   ├── backtester.py        # 回测引擎（逐K线撮合）
│   ├── portfolio.py         # 仓位管理（多/空/杠杆/保证金/强平）
│   ├── risk.py              # 风险控制（仓位计算/止损止盈/回撤熔断）
│   └── order.py             # 订单数据结构
├── strategies/              # 策略模块
│   ├── base.py              # 策略基类（继承此类编写自定义策略）
│   ├── ma_crossover.py      # 双均线交叉策略
│   ├── rsi_strategy.py      # RSI 超买超卖策略
│   ├── bollinger_bands.py   # 布林带策略
│   └── registry.py          # 策略注册表
├── analysis/                # 分析与可视化
│   ├── metrics.py           # 绩效指标（夏普/回撤/盈利因子等）
│   └── visualizer.py        # 回测图表（四联图）
├── live/
│   └── trader.py            # 实盘交易引擎（异步）
├── data/                    # K线数据本地缓存（parquet格式）
├── logs/                    # 日志文件 + 回测图表
├── backtest_runner.py       # 回测入口
├── optimize.py              # 参数网格搜索
├── main.py                  # 实盘入口
└── requirements.txt
```

---

## 快速开始

### 1. 安装依赖

```bash
# 使用项目虚拟环境
/path/to/venv/bin/pip install -r requirements.txt
```

### 2. 配置 API Key

编辑 `config/config.yaml`：

```yaml
exchange:
  api_key: "你的OKX API Key"
  api_secret: "你的OKX API Secret"
  passphrase: "你的OKX Passphrase"
  sandbox: true      # true=模拟盘，false=正式交易
```

> OKX 模拟盘需要在 OKX 网页 → 模拟交易 → API管理 里单独创建一套 Key

### 3. 运行回测

```bash
# 默认策略（config.yaml 中配置的）
python backtest_runner.py

# 指定策略和标的
python backtest_runner.py --strategy ma_crossover --symbol BTC/USDT:USDT
python backtest_runner.py --strategy rsi --symbol ETH/USDT:USDT
python backtest_runner.py --strategy bollinger_bands --symbol BTC/USDT:USDT

# 指定时间范围
python backtest_runner.py --strategy ma_crossover --start 2024-06-01 --end 2024-12-31

# 不生成图表
python backtest_runner.py --no-plot
```

### 4. 参数优化

```bash
python optimize.py --strategy ma_crossover --symbol BTC/USDT:USDT
```

自动网格搜索所有参数组合，按夏普比率排序，输出 Top 10。

### 5. 启动实盘

```bash
python main.py
python main.py --strategy ma_crossover
```

---

## 系统架构详解

### 数据流

```
OKX 公开行情接口
    ↓ fetch_ohlcv_range()
data_feed.py  →  add_indicators()  →  DataFrame（含MACD/RSI/BB等指标）
    ↓ 本地缓存（data/*.parquet）
策略模块  →  信号（1/−1/0）
    ↓
回测引擎 / 实盘引擎
    ↓
OKX 模拟盘/正式账户（下单）
```

### 信号约定

所有策略统一返回整数信号：

| 信号值 | 含义 |
|--------|------|
| `1` | 做多（开多仓，或反手平空开多） |
| `-1` | 做空（开空仓，或反手平多开空） |
| `0` | 不操作，持仓不动 |

---

## 核心模块说明

### `core/exchange.py` — 交易所连接

两个客户端分工明确：

- `public`（公开接口，无需 Key，不走 sandbox）：负责拉 K 线、获取行情
- `client`（账户接口，需要 Key）：负责查余额、下单、撤单

```python
exchange = Exchange(config)
df = exchange.fetch_ohlcv_range("BTC/USDT:USDT", "4h", "2024-01-01", "2024-12-31")
```

---

### `core/data_feed.py` — 数据获取与指标

`get_historical_data()` 自动处理缓存，第一次拉取后保存到 `data/` 目录，下次直接读本地文件。

`add_indicators()` 计算的指标包括：

| 指标 | 列名 |
|------|------|
| 简单均线 | `sma_10`, `sma_30`, `sma_50`, `sma_200` |
| 指数均线 | `ema_9`, `ema_21` |
| MACD | `macd`, `macd_signal`, `macd_hist` |
| RSI | `rsi_14` |
| 布林带 | `bb_upper`, `bb_mid`, `bb_lower`, `bb_width` |
| ATR | `atr_14` |
| 成交量均线 | `vol_sma_20` |

---

### `core/backtester.py` — 回测引擎

**合约回测完整模拟：**

- **手续费**：开仓/平仓各收一次，默认 Taker 0.05%
- **滑点**：做多时成交价上浮 0.02%，做空时下浮 0.02%
- **资金费率**：每 8 小时扣一次（4h K线 = 每 2 根），多仓付/空仓收
- **强平检测**：每根 K 线检查逐仓保证金是否耗尽
- **止损/止盈**：每根 K 线 close 价触发
- **回撤熔断**：最大回撤超过阈值时停止开新仓，平掉现有仓位

执行顺序（每根K线）：
```
快照权益 → 强平检查 → 资金费率 → 熔断检查 → 止损/止盈 → 策略信号 → 开/平仓
```

---

### `core/portfolio.py` — 仓位管理

支持多空双向持仓，每个 symbol 同时只持有一个方向。

关键方法：

```python
portfolio.apply_order(order, leverage=3)      # 应用订单到仓位
portfolio.check_liquidation(symbol, price)    # 检查强平
portfolio.deduct_funding_rate(symbol, price, rate)  # 扣资金费率
portfolio.total_equity(prices)               # 计算总权益
```

强平价格计算（逐仓模式）：
- 多仓：`entry_price × (1 - 1/leverage + 维持保证金率)`
- 空仓：`entry_price × (1 + 1/leverage - 维持保证金率)`

---

### `core/risk.py` — 风险管理

**仓位计算公式（基于固定风险）：**

```
单笔风险金额 = 总权益 × risk_per_trade_pct（默认1%）
所需保证金   = 单笔风险金额 / 止损比例
开仓数量     = 所需保证金 × 杠杆 / 当前价格
```

例：总权益 10000 USDT，止损 3%，杠杆 3x：
- 单笔风险 = 100 USDT
- 所需保证金 = 100 / 0.03 = 3333 USDT（但受 max_position_pct=20% 限制，最多用 2000 USDT）
- 实际开仓价值 = 2000 × 3 = 6000 USDT

---

### `strategies/` — 策略模块

#### 如何编写自定义策略

```python
from strategies.base import BaseStrategy
import pandas as pd

class MyStrategy(BaseStrategy):
    def __init__(self, params: dict):
        super().__init__(params)
        self.my_param = params.get("my_param", 10)

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> int:
        # df 包含截至当前 bar 的所有历史数据和指标
        # 返回 1=做多, -1=做空, 0=不操作
        if df["rsi_14"].iloc[-1] < 30:
            return 1
        if df["rsi_14"].iloc[-1] > 70:
            return -1
        return 0
```

在 `strategies/registry.py` 注册后，即可通过 `--strategy my_strategy` 调用。

#### 内置策略

**双均线交叉（ma_crossover）**
- 金叉（快线上穿慢线）→ 做多
- 死叉（快线下穿慢线）→ 做空
- 推荐参数：fast=20, slow=30（4h BTC 优化结果）

**RSI 超买超卖（rsi）**
- RSI 从超卖区（<30）回升 → 做多
- RSI 从超买区（>70）回落 → 做空

**布林带（bollinger_bands）**
- 触及下轨 + RSI 超卖 → 做多
- 触及上轨 + RSI 超买 → 做空

---

## 配置文件说明

```yaml
exchange:
  name: okx
  api_key: ""
  api_secret: ""
  passphrase: ""
  sandbox: true        # true=模拟盘
  market_type: swap    # swap=永续合约, spot=现货

trading:
  symbols:
    - BTC/USDT:USDT    # OKX 永续合约格式
  timeframe: 4h
  leverage: 3          # 杠杆倍数
  margin_mode: isolated  # isolated=逐仓, cross=全仓

risk:
  max_position_pct: 0.2      # 单笔最大保证金占比 20%
  stop_loss_pct: 0.03        # 止损 3%
  take_profit_pct: 0.06      # 止盈 6%
  max_drawdown_pct: 0.15     # 最大回撤熔断 15%
  risk_per_trade_pct: 0.01   # 单笔风险 1%

backtest:
  initial_capital: 10000.0
  fee_rate: 0.0005     # 合约 Taker 手续费 0.05%
  slippage_pct: 0.0002
  funding_rate: 0.0001 # 资金费率 0.01%/8h

strategy:
  name: ma_crossover
  params:
    fast_period: 20
    slow_period: 30
```

---

## 回测绩效指标说明

| 指标 | 含义 | 参考值 |
|------|------|--------|
| 总收益率 | 回测期间总盈亏 / 初始资金 | >0% |
| 夏普比率 | 风险调整后收益（年化） | >1 良好，>2 优秀 |
| 最大回撤 | 净值从峰值的最大跌幅 | 绝对值越小越好 |
| 卡玛比率 | 总收益率 / 最大回撤绝对值 | >1 良好 |
| 盈利因子 | 总盈利 / 总亏损 | >1.5 良好 |
| 胜率 | 盈利交易 / 总交易次数 | 趋势策略一般 25%~40% |

> 趋势跟踪策略胜率通常不高（20%~35%），但靠"赢大亏小"盈利，盈利因子比胜率更重要。

---

## 注意事项

1. **先模拟盘，再实盘**：config 中 `sandbox: true` 时使用模拟账户，确认策略稳定后再切换到 `false`
2. **杠杆风险**：合约杠杆放大收益的同时也放大亏损，建议从 2~3x 开始
3. **资金费率**：永续合约持仓过夜会产生资金费率，频繁换仓会累积手续费
4. **滑点**：实盘成交价可能与信号触发价有偏差，流动性差的标的偏差更大
5. **历史不代表未来**：回测结果仅供参考，市场状态会变化
