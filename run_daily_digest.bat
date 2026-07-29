@echo off
cd /d "D:\projects\daily-digest-bot"
set PYTHONIOENCODING=utf-8
set LOG=output\run.log

echo [%date% %time%] ======== 开始全频道运行 ======== >> %LOG%

echo [%date% %time%] --- AI 行业日报 --- >> %LOG%
python -m daily_digest.cli run --channel ai >> %LOG% 2>&1

echo [%date% %time%] --- 自动驾驶日报 --- >> %LOG%
python -m daily_digest.cli run --channel autonomous_driving >> %LOG% 2>&1

echo [%date% %time%] --- 瑞典房源日报（AI 搜索+筛选+评分）--- >> %LOG%
python housing_digest.py -o output\housing\latest.html >> %LOG% 2>&1

echo [%date% %time%] ======== 全部完成 ======== >> %LOG%
