#!/bin/bash
# ============================================================
# systemd 服务安装脚本（需要 sudo 权限）
# 用法: bash scripts/deploy.sh
# ============================================================
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$PROJECT_DIR/venv"
CURRENT_USER="$(whoami)"

echo "=== 量化交易系统 — systemd 服务安装 ==="
echo "项目目录: $PROJECT_DIR"
echo "运行用户: $CURRENT_USER"

# 1. 检查 venv
if [ ! -f "$VENV_DIR/bin/python" ]; then
    echo "错误: 虚拟环境不存在，请先执行 make install"
    exit 1
fi

# 2. 生成 service 文件
echo "[1/3] 生成 service 文件..."
sed \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__VENV_DIR__|$VENV_DIR|g" \
    -e "s|__USER__|$CURRENT_USER|g" \
    "$PROJECT_DIR/scripts/quant-trader.service.template" \
    > "/tmp/quant-trader.service"

# 3. 安装到 systemd
echo "[2/3] 安装 systemd 服务..."
sudo cp /tmp/quant-trader.service /etc/systemd/system/quant-trader.service
sudo systemctl daemon-reload
sudo systemctl enable quant-trader

# 4. 完成
echo "[3/3] 完成"
echo ""
echo "=== 接下来执行 ==="
echo "  启动服务:   sudo systemctl start quant-trader"
echo "  查看状态:   sudo systemctl status quant-trader"
echo "  查看日志:   sudo journalctl -u quant-trader -f"
echo "  停止服务:   sudo systemctl stop quant-trader"
