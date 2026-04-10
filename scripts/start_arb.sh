#!/bin/bash
# ============================================================
# 费率套利后台启动脚本
# 用法:
#   bash scripts/start_arb.sh          # 启动
#   bash scripts/start_arb.sh stop     # 停止
#   bash scripts/start_arb.sh status   # 状态
#   bash scripts/start_arb.sh restart  # 重启
# ============================================================

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$PROJECT_DIR/venv/bin/python"
PID_FILE="$PROJECT_DIR/logs/funding_arb.pid"
LOG_FILE="$PROJECT_DIR/logs/funding_arb.log"

mkdir -p "$PROJECT_DIR/logs"

start() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "已在运行，PID=$(cat $PID_FILE)"
        exit 1
    fi
    echo "启动费率套利..."
    nohup "$PYTHON" "$PROJECT_DIR/funding_arb_runner.py" >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "启动成功，PID=$(cat $PID_FILE)"
    echo "日志: tail -f $LOG_FILE"
}

stop() {
    if [ ! -f "$PID_FILE" ]; then
        echo "未运行"
        exit 0
    fi
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        rm -f "$PID_FILE"
        echo "已停止 (PID=$PID)"
    else
        rm -f "$PID_FILE"
        echo "进程不存在，清理 PID 文件"
    fi
}

status() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "运行中，PID=$(cat $PID_FILE)"
        echo ""
        tail -10 "$LOG_FILE"
    else
        echo "未运行"
    fi
}

case "${1:-start}" in
    start)   start   ;;
    stop)    stop    ;;
    status)  status  ;;
    restart) stop; sleep 2; start ;;
    *) echo "用法: $0 {start|stop|status|restart}" ;;
esac
