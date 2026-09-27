#!/bin/bash
# SMIS 监控 — 按你的环境适配
# 日志: /tmp/smis-monitor.log
# 环境变量: ~/.config/smis-monitor/env（TG_BOT_TOKEN / TG_CHAT_ID 等）

set -euo pipefail

# ===== 路径（按机器改）=====
APP_DIR="/home/code/smis-monitor"
LOG_FILE="/tmp/smis-monitor.log"
ENV_FILE="${HOME}/.config/smis-monitor/env"

# 超时（秒），防止卡死占 CPU；可用环境变量覆盖
TIMEOUT_SEC="${SMIS_TIMEOUT_SEC:-600}"
KILL_AFTER="${SMIS_KILL_AFTER:-30}"
# 清理超过 N 小时的 Playwright 临时目录
TEMP_MAX_AGE_HOURS="${SMIS_TEMP_MAX_AGE_HOURS:-1}"
# 日志超过多少 MB 轮转
LOG_MAX_MB="${SMIS_LOG_MAX_MB:-2}"

cd "$APP_DIR"

# venv（有则启用）
if [[ -f "venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
elif [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# token 等
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

cleanup_procs() {
  # 不要用 pkill -f main.py：可能误伤编辑器/其它同名进程；只清浏览器残留
  pkill -f chromium 2>/dev/null || true
  pkill -f chrome 2>/dev/null || true
  pkill -f playwright 2>/dev/null || true
}

cleanup_temps() {
  local hours="${TEMP_MAX_AGE_HOURS}"
  local mins=$((hours * 60))
  for dir in /tmp "${TMPDIR:-}"; do
    [[ -n "$dir" && -d "$dir" ]] || continue
    find "$dir" -maxdepth 1 \( \
      -name 'playwright_chromiumdev_profile-*' -o \
      -name 'playwright_chromium_profile-*' -o \
      -name 'playwright-artifacts-*' \
    \) -mmin +"${mins}" -exec rm -rf {} + 2>/dev/null || true
  done
}

rotate_log() {
  local f="$1"
  local max_bytes=$((LOG_MAX_MB * 1024 * 1024))
  [[ -f "$f" ]] || return 0
  local sz
  sz=$(stat -c%s "$f" 2>/dev/null || echo 0)
  if [[ "${sz:-0}" -lt "$max_bytes" ]]; then
    return 0
  fi
  [[ -f "${f}.3" ]] && rm -f "${f}.3"
  [[ -f "${f}.2" ]] && mv -f "${f}.2" "${f}.3"
  [[ -f "${f}.1" ]] && mv -f "${f}.1" "${f}.2"
  mv -f "$f" "${f}.1"
  : > "$f"
}

TS() { date '+%Y-%m-%d %H:%M:%S'; }

{
  echo ""
  echo "################################################################"
  echo "# run.sh 启动  $(TS)  timeout=${TIMEOUT_SEC}s"
  echo "################################################################"
} >> "$LOG_FILE" 2>&1

cleanup_procs
cleanup_temps
rotate_log "$LOG_FILE"

set +e
if command -v timeout >/dev/null 2>&1; then
  timeout --kill-after="${KILL_AFTER}" "${TIMEOUT_SEC}" python main.py >> "$LOG_FILE" 2>&1
  code=$?
else
  python main.py >> "$LOG_FILE" 2>&1
  code=$?
fi
set -e

cleanup_procs
cleanup_temps

{
  echo "################################################################"
  echo "# run.sh 结束  $(TS)  exit=${code}"
  echo "################################################################"
  echo ""
} >> "$LOG_FILE" 2>&1

if [[ "$code" -eq 124 ]]; then
  echo "[run.sh] 超时 ${TIMEOUT_SEC}s，已终止" >> "$LOG_FILE"
fi

exit "$code"
