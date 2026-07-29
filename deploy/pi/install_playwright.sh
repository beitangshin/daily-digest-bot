#!/usr/bin/env bash
# Install Playwright + Chromium for the housing channel (Booli scraper).
# Run this AFTER deploy/pi/install.sh.
#
# Usage:
#   cd ~/daily-digest-bot && bash deploy/pi/install_playwright.sh
set -euo pipefail

REPO_DIR="$(pwd)"

if [ ! -f "$REPO_DIR/.venv/bin/python" ]; then
  echo "❌ 没找到 .venv，请先跑 deploy/pi/install.sh" >&2
  exit 1
fi

echo "==> 安装 Playwright Python 包"
"$REPO_DIR/.venv/bin/pip" install playwright

echo "==> 安装 Chromium 浏览器（~300MB 下载）"
"$REPO_DIR/.venv/bin/playwright" install chromium

echo ""
echo "✅ Playwright + Chromium 安装完成"
echo "   房源频道（Booli 爬虫）现在可以跑了"
echo "   测试: $REPO_DIR/.venv/bin/python booli_scraper.py --city Stockholm --pages 1)"
