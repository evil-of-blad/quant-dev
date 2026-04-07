# ============================================================
# 量化交易系统 — 快捷命令
# 用法: make <命令>
# ============================================================

PYTHON   := venv/bin/python
PIP      := venv/bin/pip
SYMBOL   := BTC/USDT:USDT
STRATEGY := bollinger_bands

# ---------- 安装 ----------
install:
	python3 -m venv venv
	$(PIP) install -r requirements.txt

# ---------- 回测 ----------
backtest:
	$(PYTHON) backtest_runner.py --strategy $(STRATEGY) --symbol $(SYMBOL)

backtest-2024:
	$(PYTHON) backtest_runner.py --strategy $(STRATEGY) --symbol $(SYMBOL) --start 2024-01-01 --end 2024-12-31

backtest-2025:
	$(PYTHON) backtest_runner.py --strategy $(STRATEGY) --symbol $(SYMBOL) --start 2025-01-01 --end 2025-12-31

backtest-all:
	$(PYTHON) backtest_runner.py --strategy $(STRATEGY) --symbol $(SYMBOL) --start 2024-01-01 --end 2025-12-31

# ---------- 参数优化 ----------
optimize:
	$(PYTHON) optimize.py --strategy $(STRATEGY) --symbol $(SYMBOL) --multi-period

# ---------- 实盘 ----------
live:
	$(PYTHON) main.py --strategy $(STRATEGY)

# ---------- 后台启动 / 停止 / 状态 ----------
start:
	bash scripts/start.sh start

stop:
	bash scripts/start.sh stop

status:
	bash scripts/start.sh status

restart:
	bash scripts/start.sh restart

# ---------- 日志 ----------
log:
	tail -f logs/quant.log

# ---------- systemd 服务（需要 root）----------
service-install:
	bash scripts/deploy.sh

service-start:
	sudo systemctl start quant-trader

service-stop:
	sudo systemctl stop quant-trader

service-status:
	sudo systemctl status quant-trader

service-log:
	sudo journalctl -u quant-trader -f

.PHONY: install backtest backtest-2024 backtest-2025 backtest-all optimize live \
        start stop status restart log \
        service-install service-start service-stop service-status service-log
