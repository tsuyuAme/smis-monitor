@echo off
cd /d "%~dp0"

REM === EDIT YOUR TOKEN BELOW ===
set "TG_BOT_TOKEN=PUT_YOUR_BOT_TOKEN_HERE"
set "TG_CHAT_ID=PUT_YOUR_CHAT_ID_HERE"

set "SMIS_HEADED=0"

python main.py
if errorlevel 1 (
  echo Failed. Try run_setup.bat again.
)
