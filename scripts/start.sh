#!/bin/bash
# ============================================================
# 布林带策略后台启动脚本（强化版）
# - 优雅停止 + 超时强杀
# - 启动前清理所有残留进程
# 用法:
#   bash scripts/start.sh           # 启动
#   bash scripts/start.sh stop      # 停止
#   bash scripts/start.sh status    # 状态
#   bash scripts/start.sh restart   # 重启
# ============================================================

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$PROJECT_DIR/venv/bin/python"
SCRIPT="$PROJECT_DIR/main.py"
PID_FILE="$PROJECT_DIR/logs/quant.pid"
LOG_FILE="$PROJECT_DIR/logs/quant.log"
PROC_PATTERN="main.py.*bollinger_bands"

mkdir -p "$PROJECT_DIR/logs"

find_pids() {
    pgrep -f "$PROC_PATTERN" 2>/dev/null | tr '\n' ' '
}

kill_pid() {
    local pid=$1
    local timeout=15

    if ! kill -0 "$pid" 2>/dev/null; then
        return 0
    fi

    echo "  发送 SIGTERM 到 PID $pid..."
    kill -TERM "$pid" 2>/dev/null

    local i=0
    while [ $i -lt $timeout ]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "  PID $pid 已优雅退出"
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done

    echo "  ⚠️ PID $pid 在 ${timeout}s 内未退出，发送 SIGKILL"
    kill -KILL "$pid" 2>/dev/null
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
        return 1
    fi
    echo "  PID $pid 已强制终止"
}

start() {
    local existing
    existing=$(find_pids)
    if [ -n "$existing" ]; then
        echo "❌ 检测到残留进程: $existing"
        echo "   请先执行 stop 清理"
        exit 1
    fi

    echo "启动布林带策略..."
    nohup "$PYTHON" "$SCRIPT" --strategy bollinger_bands >> "$LOG_FILE" 2>&1 &
    local new_pid=$!
    echo $new_pid > "$PID_FILE"

    sleep 2
    if ! kill -0 "$new_pid" 2>/dev/null; then
        echo "❌ 启动失败，查看日志: tail -50 $LOG_FILE"
        rm -f "$PID_FILE"
        exit 1
    fi
    echo "✅ 启动成功，PID=$new_pid"
    echo "   日志: tail -f $LOG_FILE"
}

stop() {
    local pids
    pids=$(find_pids)

    if [ -z "$pids" ] && [ ! -f "$PID_FILE" ]; then
        echo "未运行"
        return 0
    fi

    if [ -n "$pids" ]; then
        echo "找到运行中的进程: $pids"
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
    local count
    count=$(echo "$pids" | wc -w)
    if [ "$count" -gt 1 ]; then
        echo "⚠️  发现 $count 个进程（异常！）: $pids"
    else
        echo "运行中，PID=$pids"
    fi
    echo ""
    echo "--- CPU/内存占用 ---"
    for pid in $pids; do
        ps -p "$pid" -o pid,pcpu,pmem,etime,cmd 2>/dev/null | tail -n +2
    done
    echo ""
    echo "--- 最近 10 条日志 ---"
    tail -10 "$LOG_FILE"
}

case "${1:-start}" in
    start)   start   ;;
    stop)    stop    ;;
    status)  status  ;;
    restart) stop; sleep 1; start ;;
    *) echo "用法: $0 {start|stop|status|restart}" ;;
esac
