@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM ========== 首次配置：改成你的 ==========
set TG_BOT_TOKEN=这里填BotToken
set TG_CHAT_ID=这里填ChatID

REM 弹出浏览器，给你 90 秒过验证码
set SMIS_SETUP=1
set SMIS_MANUAL_WAIT=90

echo.
echo ========================================
echo  首次设置：会弹出浏览器窗口
echo  1. 如有验证码请完成
echo  2. 确认表格里已经有挂刀数据（不是 No Data）
echo  3. 等倒计时结束或直接看终端输出
echo  配置会保存在 browser_data 文件夹
echo ========================================
echo.

python main.py
echo.
pause
