@echo off
cd /d "D:\projects\daily-digest-bot"
echo [%date% %time%] 开始房源搜索 >> output\housing_run.log
"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe" housing_digest.py -o output\housing\housing_latest.html >> output\housing_run.log 2>&1
echo [%date% %time%] 完成 >> output\housing_run.log
