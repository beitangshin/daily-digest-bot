#!/usr/bin/env bash
# Wrapper script: runs ALL three channels in sequence.
# Called by daily-digest.service (systemd oneshot).
#
# 为什么不会漏信息?
# 主管道 (daily_digest.cli) 在每个频道的 output/<channel>/.state.json
# 里记录了上次成功运行的时间戳，fetch 阶段自动抓"自上次以来"的内容。
# 即使定时器因关机错过，Persistent=true 会在开机时补跑，.state.json
# 保证窗口连续不重叠。详见 src/daily_digest/state.py 和
# src/daily_digest/orchestrator.py 的 _determine_cutoff()。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
VENV_PYTHON="$REPO_DIR/.venv/bin/python"
LOG="$REPO_DIR/output/run.log"
cd "$REPO_DIR"

echo "[$(date)] ======== 全频道运行开始 ========" >> "$LOG"

echo "[$(date)] --- AI 行业日报 ---" >> "$LOG"
"$VENV_PYTHON" -m daily_digest.cli run --channel ai >> "$LOG" 2>&1

echo "[$(date)] --- 自动驾驶日报 ---" >> "$LOG"
"$VENV_PYTHON" -m daily_digest.cli run --channel autonomous_driving >> "$LOG" 2>&1

echo "[$(date)] --- 瑞典房源日报（Playwright 爬虫 + AI 评分）---" >> "$LOG"
"$VENV_PYTHON" "$REPO_DIR/housing_digest.py" -o "$REPO_DIR/output/housing/latest.html" >> "$LOG" 2>&1
echo "[$(date)] ======== 全频道运行结束 ========" >> "$LOG"
