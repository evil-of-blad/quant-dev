#!/bin/bash
# ============================================================
# 后台启动脚本（适用于 Mac / 无 systemd 权限的 Linux 服务器）
# 用法:
#   bash scripts/start.sh          # 启动
#   bash scripts/start.sh stop     # 停止
#   bash scripts/start.sh status   # 查看状态
#   bash scripts/start.sh restart  # 重启
# ============================================================

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$PROJECT_DIR/venv/bin/python"
PID_FILE="$PROJECT_DIR/logs/quant.pid"
LOG_FILE="$PROJECT_DIR/logs/quant.log"

mkdir -p "$PROJECT_DIR/logs"

start() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "已在运行，PID=$(cat $PID_FILE)。先执行 stop 再启动。"
        exit 1
    fi
    echo "启动量化交易机器人..."
    nohup "$PYTHON" "$PROJECT_DIR/main.py" --strategy bollinger_bands \
        >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "启动成功，PID=$(cat $PID_FILE)"
    echo "查看日志: tail -f $LOG_FILE"
}

stop() {
    if [ ! -f "$PID_FILE" ]; then
        echo "未找到 PID 文件，机器人可能未在运行"
        exit 0
    fi
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        rm -f "$PID_FILE"
        echo "已停止 (PID=$PID)"
    else
        echo "进程 $PID 不存在，清理 PID 文件"
        rm -f "$PID_FILE"
    fi
}

status() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "运行中，PID=$(cat $PID_FILE)"
        echo ""
        echo "--- 最近 20 条日志 ---"
        tail -20 "$LOG_FILE"
    else
        echo "未运行"
    fi
}

case "${1:-start}" in
    start)   start   ;;
    stop)    stop    ;;
    status)  status  ;;
    restart) stop; sleep 2; start ;;
    *)
        echo "用法: $0 {start|stop|status|restart}"
        exit 1
    ;;
esac
