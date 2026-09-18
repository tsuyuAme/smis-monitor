#!/bin/bash
cd /home/code/smis-monitor   # 改成你的路径
source venv/bin/activate
source ~/.config/smis-monitor/env
python main.py >> /tmp/smis-monitor.log 2>&1
pkill -f 'main.py' || true
pkill -f chromium || true
pkill -f chrome || true
pkill -f playwright || true