# 服务器部署教程

## 目录
1. [上传代码到服务器](#1-上传代码到服务器)
2. [服务器环境准备](#2-服务器环境准备)
3. [配置 API Key](#3-配置-api-key)
4. [运行回测确认环境正常](#4-运行回测确认环境正常)
5. [方式一：nohup 后台运行（简单）](#5-方式一nohup-后台运行简单)
6. [方式二：systemd 服务（推荐）](#6-方式二systemd-服务推荐)
7. [日常操作速查](#7-日常操作速查)

---

## 1. 上传代码到服务器

在本地机器执行，把整个项目传到服务器：

```bash
# 把 quant-dev 目录上传到服务器（替换 user 和 your-server-ip）
scp -r /Users/mi/Desktop/workspace/quant-dev user@your-server-ip:~/quant-dev
```

或者用 Git：

```bash
# 服务器上执行
git clone https://your-repo-url.git ~/quant-dev
```

---

## 2. 服务器环境准备

登录服务器后，进入项目目录：

```bash
cd ~/quant-dev
```

**安装 Python（如果没有）：**

```bash
# Ubuntu / Debian
sudo apt update && sudo apt install -y python3 python3-pip python3-venv

# CentOS / AlmaLinux
sudo yum install -y python3 python3-pip
```

**创建虚拟环境并安装依赖：**

```bash
make install
```

> 如果服务器上没有 `make`，手动执行：
> ```bash
> python3 -m venv venv
> venv/bin/pip install -r requirements.txt
> ```

---

## 3. 配置 API Key

编辑配置文件：

```bash
nano config/config.yaml
```

找到 exchange 部分，填入你的 OKX API 信息：

```yaml
exchange:
  api_key: "你的OKX API Key"
  api_secret: "你的OKX API Secret"
  passphrase: "你的OKX Passphrase"
  sandbox: true    # 先用模拟盘，没问题再改 false
```

保存退出：`Ctrl+O` → `Enter` → `Ctrl+X`

---

## 4. 运行回测确认环境正常

先跑一次回测，验证代码和 API 连接都没问题：

```bash
make backtest-all
```

看到类似如下输出说明正常：

```
回测完成: 最终权益 13670.21, 收益率 36.70%, 强平次数 0
```

---

## 5. 方式一：nohup 后台运行（简单）

适合没有 root 权限，或者临时测试的场景。

**启动：**

```bash
make start
# 或
bash scripts/start.sh start
```

**查看状态：**

```bash
make status
# 或
bash scripts/start.sh status
```

**实时查看日志：**

```bash
make log
# 或
tail -f logs/quant.log
```

**停止：**

```bash
make stop
# 或
bash scripts/start.sh stop
```

**重启：**

```bash
make restart
```

> **注意：** nohup 方式在你退出 SSH 后仍会继续运行，但服务器重启后需要手动重新启动。

---

## 6. 方式二：systemd 服务（推荐）

适合正式运行，服务器重启后自动拉起，崩溃后自动重启（30秒内）。

**需要 sudo 权限。**

**一键安装：**

```bash
make service-install
# 或
bash scripts/deploy.sh
```

**启动服务：**

```bash
make service-start
# 或
sudo systemctl start quant-trader
```

**查看运行状态：**

```bash
make service-status
# 或
sudo systemctl status quant-trader
```

正常运行时输出类似：

```
● quant-trader.service - 量化交易机器人 (OKX MA Crossover)
     Loaded: loaded (/etc/systemd/system/quant-trader.service; enabled)
     Active: active (running) since ...
```

**实时查看日志（两种方式）：**

```bash
# 方式A：systemd 日志
make service-log
sudo journalctl -u quant-trader -f

# 方式B：直接查看日志文件
tail -f logs/quant.log
```

**停止服务：**

```bash
make service-stop
sudo systemctl stop quant-trader
```

**服务器重启后自动恢复：** 已通过 `systemctl enable` 设置，无需手动操作。

---

## 7. 日常操作速查

| 操作 | 命令 |
|------|------|
| 安装依赖 | `make install` |
| 回测（全部年份） | `make backtest-all` |
| 回测 2024 | `make backtest-2024` |
| 回测 2025 | `make backtest-2025` |
| 参数优化 | `make optimize` |
| 启动实盘（前台） | `make live` |
| 后台启动 | `make start` |
| 后台停止 | `make stop` |
| 查看后台状态 | `make status` |
| 查看日志 | `make log` |
| systemd 启动 | `make service-start` |
| systemd 停止 | `make service-stop` |
| systemd 状态 | `make service-status` |
| systemd 日志 | `make service-log` |

---

## 注意事项

1. **先模拟盘，再实盘**：`config.yaml` 中 `sandbox: true` 跑稳定后再改 `false`
2. **服务器时区**：建议设置为 UTC，避免 K 线时间对齐问题
   ```bash
   sudo timedatectl set-timezone UTC
   ```
3. **磁盘空间**：日志文件会持续增长，建议定期清理或配置 logrotate
4. **防火墙**：交易程序只需出站访问 OKX API，不需要开放入站端口
5. **切换正式盘**：修改 `config/config.yaml` 中 `sandbox: false`，然后重启服务
