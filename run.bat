@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM ========== 日常定时跑（可被任务计划调用）==========
set TG_BOT_TOKEN=这里填BotToken
set TG_CHAT_ID=这里填ChatID

REM 默认无头，复用 browser_data 里已过验证的配置
REM 若经常失败，可改成 set SMIS_HEADED=1
set SMIS_HEADED=0

python main.py
if errorlevel 1 (
  echo 运行失败，可改用 run_setup.bat 重新过一次验证
)
