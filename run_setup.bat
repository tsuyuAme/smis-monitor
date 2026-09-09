@echo off
cd /d "%~dp0"

REM === EDIT YOUR TOKEN BELOW ===
set "TG_BOT_TOKEN=PUT_YOUR_BOT_TOKEN_HERE"
set "TG_CHAT_ID=PUT_YOUR_CHAT_ID_HERE"

set "SMIS_SETUP=1"
set "SMIS_MANUAL_WAIT=90"

echo.
echo ========================================
echo  First-time setup: browser will open
echo  1. Complete captcha if any
echo  2. Wait until table has data
echo  3. Profile saved in browser_data
echo ========================================
echo.

python main.py
echo.
pause
