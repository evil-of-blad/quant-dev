#!/bin/bash
# ============================================================
# 市场告警引擎后台启动脚本
# ============================================================

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$PROJECT_DIR/venv/bin/python"
SCRIPT="$PROJECT_DIR/alert_runner.py"
PID_FILE="$PROJECT_DIR/logs/alert.pid"
LOG_FILE="$PROJECT_DIR/logs/alert.log"
PROC_PATTERN="alert_runner.py"

mkdir -p "$PROJECT_DIR/logs"

find_pids() {
    pgrep -f "$PROC_PATTERN" 2>/dev/null | tr '\n' ' '
}

kill_pid() {
    local pid=$1
    local timeout=15
    if ! kill -0 "$pid" 2>/dev/null; then return 0; fi
    echo "  发送 SIGTERM 到 PID $pid..."
    kill -TERM "$pid" 2>/dev/null
    local i=0
    while [ $i -lt $timeout ]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "  PID $pid 已退出"
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    echo "  超时，强杀"
    kill -KILL "$pid" 2>/dev/null
}

start() {
    local existing
    existing=$(find_pids)
    if [ -n "$existing" ]; then
        echo "❌ 已有进程: $existing"
        exit 1
    fi
    echo "启动告警引擎..."
    nohup "$PYTHON" "$SCRIPT" >> "$LOG_FILE" 2>&1 &
    local new_pid=$!
    echo $new_pid > "$PID_FILE"
    sleep 2
    if ! kill -0 "$new_pid" 2>/dev/null; then
        echo "❌ 启动失败，查看日志: tail -50 $LOG_FILE"
        rm -f "$PID_FILE"
        exit 1
    fi
    echo "✅ 启动成功，PID=$new_pid"
}

stop() {
    local pids
    pids=$(find_pids)
    if [ -z "$pids" ] && [ ! -f "$PID_FILE" ]; then
        echo "未运行"
        return 0
    fi
    if [ -n "$pids" ]; then
        echo "找到进程: $pids"
        for pid in $pids; do
            kill_pid "$pid"
        done
    fi
    rm -f "$PID_FILE"
    echo "✅ 已停止"
}

status() {
    local pids
    pids=$(find_pids)
    if [ -z "$pids" ]; then
        echo "未运行"
        return
    fi
    echo "运行中，PID=$pids"
    echo ""
    for pid in $pids; do
        ps -p "$pid" -o pid,pcpu,pmem,etime,cmd 2>/dev/null | tail -n +2
    done
    echo ""
    tail -10 "$LOG_FILE"
}

case "${1:-start}" in
    start)   start   ;;
    stop)    stop    ;;
    status)  status  ;;
    restart) stop; sleep 1; start ;;
    *) echo "用法: $0 {start|stop|status|restart}" ;;
esac
