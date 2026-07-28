#!/usr/bin/env bash
# Installs daily-digest-bot on a Raspberry Pi as two systemd units:
#   daily-digest.timer       -- runs `daily-digest run` once a day at 08:00
#   daily-digest-web.service -- serves output/ at http://<pi-ip>:<port> (always on)
#
# Run this from inside the cloned repo directory:
#   cd ~/daily-digest-bot && bash deploy/pi/install.sh [port]
#
# Needs sudo (to install the systemd units and enable the services).
set -euo pipefail

REPO_DIR="$(pwd)"
RUN_USER="$(whoami)"
PYTHON_BIN="$(command -v python3)"
PORT="${1:-8080}"

if [ ! -f "$REPO_DIR/pyproject.toml" ]; then
  echo "请在 clone 下来的 daily-digest-bot 仓库根目录里运行这个脚本" >&2
  exit 1
fi

echo "==> 安装系统依赖（python3-venv，以及万一 lxml 在这台设备上没有现成 wheel、需要本地编译时用到的头文件）"
sudo apt-get update
sudo apt-get install -y python3-venv python3-dev build-essential libxml2-dev libxslt1-dev zlib1g-dev

echo "==> 创建虚拟环境并安装依赖"
python3 -m venv "$REPO_DIR/.venv"
"$REPO_DIR/.venv/bin/pip" install -e "$REPO_DIR"

mkdir -p "$REPO_DIR/output"

if [ ! -f "$REPO_DIR/.env" ]; then
  cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
  echo "==> 已生成 $REPO_DIR/.env，请编辑填入 DEEPSEEK_API_KEY 后再跑 daily-digest.service"
fi

echo "==> 写入 systemd unit 文件 (/etc/systemd/system/)"

sudo tee /etc/systemd/system/daily-digest.service > /dev/null <<EOF
[Unit]
Description=Daily Digest Bot - run once
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$REPO_DIR
ExecStart=$REPO_DIR/.venv/bin/python -m daily_digest.cli run
EOF

sudo tee /etc/systemd/system/daily-digest.timer > /dev/null <<EOF
[Unit]
Description=Run Daily Digest Bot every day at 08:00

[Timer]
OnCalendar=*-*-* 08:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo tee /etc/systemd/system/daily-digest-web.service > /dev/null <<EOF
[Unit]
Description=Daily Digest Bot - serve output/ over the local network
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
EOF

echo "==> 启用并启动服务"
sudo systemctl daemon-reload
sudo systemctl enable --now daily-digest-web.service
sudo systemctl enable --now daily-digest.timer

PI_IP="$(hostname -I | awk '{print $1}')"
echo
echo "==> 完成。"
echo "    网页地址: http://$PI_IP:$PORT"
echo "    今天先手动跑一次试试（会真实调用 DeepSeek，确认 .env 填好了 key）:"
echo "      sudo systemctl start daily-digest.service"
echo "    看日志:"
echo "      journalctl -u daily-digest.service -f"
echo "      journalctl -u daily-digest-web.service -f"
echo "    定时器状态/下次触发时间:"
echo "      systemctl list-timers daily-digest.timer"
