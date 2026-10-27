#!/usr/bin/env bash
# macOS/Linux counterpart of restart-dev.ps1: stop this checkout's dev stack, free the dev ports,
# start the API and the desktop host again, and print the mode check.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/dev-common.sh"

TIMEOUT_SECONDS=70
STATUS_LOG="$LOG_DIR/restart-status.log"
echo "Lumen restart started: $(date +%Y-%m-%dT%H:%M:%S)" >"$STATUS_LOG"
report() {
  echo "$1" | tee -a "$STATUS_LOG"
}

listener_pids() {
  local ports
  ports="$(IFS=,; echo "${DEV_PORTS[*]}")"
  lsof -nP -t -iTCP:"$ports" -sTCP:LISTEN 2>/dev/null | sort -u || true
}

dev_command_pids() {
  pgrep -f "uvicorn app.main:app.*--port 8001" 2>/dev/null || true
  pgrep -f "$REPO_ROOT/apps/desktop/scripts/dev.mjs" 2>/dev/null || true
  pgrep -f "scripts/dev.mjs" 2>/dev/null | while read -r pid; do
    [[ "$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')" == "$REPO_ROOT/apps/desktop" ]] && echo "$pid"
  done
  desktop_pid
}

# A pid plus all of its descendants.
with_descendants() {
  local pid="$1"
  echo "$pid"
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    with_descendants "$child"
  done
}

stop_dev_stack() {
  # Never stop this script or the shells that started it.
  local protected=" $$ " p=$$
  while [[ "$p" -gt 1 ]]; do
    p="$(ps -o ppid= -p "$p" | tr -d ' ')"
    [[ -z "$p" ]] && break
    protected+="$p "
  done

  local targets=()
  for pid in $(listener_pids) $(dev_command_pids); do
    for each in $(with_descendants "$pid"); do
      [[ "$protected" == *" $each "* ]] || targets+=("$each")
    done
  done

  if (( ${#targets[@]} > 0 )); then
    local unique
    unique="$(printf '%s\n' "${targets[@]}" | sort -un | tr '\n' ' ')"
    kill $unique 2>/dev/null || true
    local deadline=$(( $(date +%s) + 12 ))
    while (( $(date +%s) < deadline )) && [[ -n "$(listener_pids)" ]]; do sleep 1; done
    kill -9 $unique 2>/dev/null || true
    report "Stopped dev processes: $unique"
  else
    report "No existing dev processes found."
  fi

  if [[ -n "$(listener_pids)" ]]; then
    report "Could not clear dev ports: $(listener_pids | tr '\n' ' ')"
    exit 1
  fi
}

start_dev_stack() {
  cd "$REPO_ROOT"
  if [[ "$(uname)" == "Darwin" ]]; then
    # The host starts the API itself (see open_desktop_host_macos in dev-common.sh).
    npm --workspace apps/desktop run build:renderer >"$LOG_DIR/desktop-restart.out.log" 2>"$LOG_DIR/desktop-restart.err.log"
    open_desktop_host_macos
    report "Logs: $LOG_DIR"
    return
  fi
  nohup npm run api:dev >"$LOG_DIR/api-restart.out.log" 2>"$LOG_DIR/api-restart.err.log" &
  sleep 2
  nohup npm run desktop:dev >"$LOG_DIR/desktop-restart.out.log" 2>"$LOG_DIR/desktop-restart.err.log" &
  report "Logs: $LOG_DIR"
}

mode_check() {
  local output
  if ! output="$(python "$REPO_ROOT/apps/api/mode_check.py")"; then
    report "Mode check failed. Output: $output"
    exit 1
  fi
  python - "$output" <<'PY' | while read -r line; do report "$line"; done
import json, sys
mode = json.loads(sys.argv[1])
print(f"Mode: model_mode={mode.get('model_mode')}, profile_mode={mode.get('profile_mode')}, resume_mode={mode.get('resume_mode')}")
print(f"Warnings: {', '.join(mode.get('warnings') or []) or 'none'}")
PY
}

stop_dev_stack
start_dev_stack

deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))
wait_for_http_ok "API" "$API_HEALTH_URL" "$deadline" | tee -a "$STATUS_LOG"
wait_for_desktop $(( deadline + TIMEOUT_SECONDS * 2 )) | tee -a "$STATUS_LOG"
mode_check

listeners="$(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | awk -v ports=" ${DEV_PORTS[*]} " '{n=split($9,a,":"); if (index(ports, " " a[n] " ")) print a[n] "=pid:" $2}' | sort -u | tr '\n' ' ')"
report "Listeners: $listeners"
report "Lumen desktop stack restarted."
