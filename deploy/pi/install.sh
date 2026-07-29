#!/usr/bin/env bash
# Raspberry Pi 一键部署: daily-digest-bot 三个频道 + 本地网页服务。
#
# 安装三个 systemd 单元:
#   daily-digest.timer       每天 08:00 + 20:00 跑全频道（AI+自动驾驶+房源）
#   daily-digest-web.service 开机自启 HTTP 服务，局域网访问
#
# 用法:
#   cd ~/daily-digest-bot && bash deploy/pi/install.sh [port]
#
# 前置条件:
#   - Raspberry Pi OS (Bookworm/Raspbian) 或 Ubuntu Server
#   - sudo 权限
#   - 已 git clone 本仓库到 Pi 上
#
# 首次部署后:
#   1. 编辑 .env 填入 DEEPSEEK_API_KEY
#   2. 安装 Playwright（房源频道需要）:
#      bash deploy/pi/install_playwright.sh
#   3. 手动跑一次:
#      sudo systemctl start daily-digest.service
set -euo pipefail

REPO_DIR="$(pwd)"
RUN_USER="$(whoami)"
PYTHON_BIN="$(command -v python3)"
PORT="${1:-8080}"

if [ ! -f "$REPO_DIR/pyproject.toml" ]; then
  echo "❌ 请在 daily-digest-bot 仓库根目录运行此脚本" >&2
  exit 1
fi

echo "========================================"
echo " daily-digest-bot 树莓派部署"
echo " 仓库: $REPO_DIR"
echo " 用户: $RUN_USER"
echo " 端口: $PORT"
echo "========================================"

# ---- 系统依赖 ----
echo ""
echo "==> 安装系统依赖"
sudo apt-get update -qq
sudo apt-get install -y -qq \
  python3-venv python3-dev \
  build-essential \
  libxml2-dev libxslt1-dev \
  zlib1g-dev libffi-dev libssl-dev

# ---- Python 虚拟环境 ----
echo ""
echo "==> 创建 Python 虚拟环境"
python3 -m venv "$REPO_DIR/.venv"
"$REPO_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$REPO_DIR/.venv/bin/pip" install --quiet -e "$REPO_DIR"

# ---- 输出目录 ----
mkdir -p "$REPO_DIR/output/ai"
mkdir -p "$REPO_DIR/output/autonomous_driving"
mkdir -p "$REPO_DIR/output/housing"

# ---- .env ----
if [ ! -f "$REPO_DIR/.env" ]; then
  if [ -f "$REPO_DIR/.env.example" ]; then
    cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
  else
    echo "DEEPSEEK_API_KEY=sk-your-key-here" > "$REPO_DIR/.env"
  fi
  echo "⚠️  已创建 .env，请编辑填入 DEEPSEEK_API_KEY"
fi

# ---- 可执行权限 ----
chmod +x "$REPO_DIR/deploy/pi/run_all.sh"
chmod +x "$REPO_DIR/housing_digest.py"

# ---- 写入 systemd 文件 ----
echo ""
echo "==> 写入 systemd 单元文件"

# 全频道运行 wrapper（放在 /usr/local/bin 避免 systemd 里出现 $REPO_DIR 路径）
sudo tee /usr/local/bin/daily-digest-runner > /dev/null <<WRAPPEREOF
#!/usr/bin/env bash
exec sudo -u $RUN_USER bash $REPO_DIR/deploy/pi/run_all.sh
WRAPPEREOF
sudo chmod +x /usr/local/bin/daily-digest-runner

# daily-digest.service — 被 timer 触发的 oneshot
sudo tee /etc/systemd/system/daily-digest.service > /dev/null <<SERVICEEOF
[Unit]
Description=Daily Digest Bot — 全频道运行（AI + 自动驾驶 + 房源）
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/daily-digest-runner
SERVICEEOF

# daily-digest.timer — 每天 08:00 + 20:00
sudo tee /etc/systemd/system/daily-digest.timer > /dev/null <<TIMEREOF
[Unit]
Description=Daily Digest Bot — 每日定时运行（08:00 / 20:00）

[Timer]
OnCalendar=*-*-* 08:00:00
OnCalendar=*-*-* 20:00:00
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
TIMEREOF

# daily-digest-web.service — 开机自启 HTTP
sudo tee /etc/systemd/system/daily-digest-web.service > /dev/null <<WEBEOF
[Unit]
Description=Daily Digest Bot — 局域网网页服务（output/）
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$REPO_DIR/output
ExecStart=$PYTHON_BIN -m http.server $PORT --bind 0.0.0.0
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
WEBEOF

# ---- 启动 ----
echo ""
echo "==> 启用并启动服务"
sudo systemctl daemon-reload
sudo systemctl enable --now daily-digest-web.service 2>/dev/null || true
sudo systemctl enable --now daily-digest.timer 2>/dev/null || true

# ---- 完成提示 ----
PI_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo ""
echo "========================================"
echo " ✅ 部署完成！"
echo ""
echo "  📖 网页地址: http://${PI_IP:-树莓派IP}:$PORT"
echo ""
echo "  ⚠️  接下来:"
echo "    1. 编辑 .env 填入 DeepSeek API key:"
echo "       nano $REPO_DIR/.env"
echo ""
echo "    2. 安装 Playwright（房源频道需要）:"
echo "       $REPO_DIR/.venv/bin/pip install playwright"
echo "       $REPO_DIR/.venv/bin/playwright install chromium"
echo "       或: bash deploy/pi/install_playwright.sh"
echo ""
echo "    3. 手动跑一次验证:"
echo "       sudo systemctl start daily-digest.service"
echo "       journalctl -u daily-digest.service -f"
echo ""
echo "  📋 常用命令:"
echo "    下次触发时间: systemctl list-timers daily-digest.timer"
echo "    网页日志:     journalctl -u daily-digest-web.service -f"
echo "    手动跑:      sudo systemctl start daily-digest.service"
echo "========================================"
