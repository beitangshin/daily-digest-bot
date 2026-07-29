#!/usr/bin/env bash
# Wrapper script: runs ALL three channels in sequence.
# Called by daily-digest.service (systemd oneshot).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
VENV_PYTHON="$REPO_DIR/.venv/bin/python"
LOG="$REPO_DIR/output/run.log"

echo "[$(date)] ======== 全频道运行开始 ========" >> "$LOG"

echo "[$(date)] --- AI 行业日报 ---" >> "$LOG"
"$VENV_PYTHON" -m daily_digest.cli run --channel ai >> "$LOG" 2>&1

echo "[$(date)] --- 自动驾驶日报 ---" >> "$LOG"
"$VENV_PYTHON" -m daily_digest.cli run --channel autonomous_driving >> "$LOG" 2>&1

echo "[$(date)] --- 瑞典房源日报（Playwright 爬虫 + AI 评分）---" >> "$LOG"
"$VENV_PYTHON" housing_digest.py -o "$REPO_DIR/output/housing/latest.html" >> "$LOG" 2>&1

echo "[$(date)] ======== 全频道运行结束 ========" >> "$LOG"
