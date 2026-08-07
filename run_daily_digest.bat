@echo off
chcp 65001 >nul
cd /d "D:\projects\daily-digest-bot"
powershell -NoProfile -ExecutionPolicy Bypass -File "D:\projects\daily-digest-bot\run_daily_digest.ps1"
