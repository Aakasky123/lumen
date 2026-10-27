#!/usr/bin/env bash
# macOS/Linux counterpart of dev-start.ps1: start the API (reusing a healthy one), then the desktop host.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/dev-common.sh"

TIMEOUT_SECONDS=60
started=()
cleanup_on_error() {
  for pid in "${started[@]:-}"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done
}
trap 'cleanup_on_error' ERR

cd "$REPO_ROOT"

if [[ "$(uname)" == "Darwin" ]]; then
  # The host starts the API itself, so both run as Lumen's dev host for macOS permissions (dev-common.sh).
  if [[ -n "$(desktop_pid)" ]]; then
    echo "Lumen desktop is already running."
  else
    if http_ok "$API_HEALTH_URL"; then
      echo "An API is already running on 8001; the desktop host will reuse it (macOS charges its permissions to whatever started it). Use npm run restart to start both fresh." >&2
    fi
    echo "Building the cockpit..."
    npm --workspace apps/desktop run build:renderer >"$LOG_DIR/desktop-dev.out.log" 2>"$LOG_DIR/desktop-dev.err.log"
    echo "Starting Lumen desktop (it starts the API)..."
    open_desktop_host_macos
    wait_for_http_ok "API" "$API_HEALTH_URL" $(( $(date +%s) + TIMEOUT_SECONDS ))
    wait_for_desktop $(( $(date +%s) + TIMEOUT_SECONDS ))
  fi
  echo "Lumen desktop stack is ready."
  echo "API: $API_URL"
  echo "Logs: $LOG_DIR (the API's own log: ~/Library/Application Support/Electron/logs/lumen-api.log)"
  exit 0
fi

if http_ok "$API_HEALTH_URL"; then
  echo "API is already healthy at $API_HEALTH_URL"
else
  echo "Starting API on 127.0.0.1:8001..."
  nohup npm run api:dev >"$LOG_DIR/api-dev.out.log" 2>"$LOG_DIR/api-dev.err.log" &
  api_pid=$!
  started+=("$api_pid")
  wait_for_http_ok "API" "$API_HEALTH_URL" $(( $(date +%s) + TIMEOUT_SECONDS )) "$api_pid"
fi

if [[ -n "$(desktop_pid)" ]]; then
  echo "Lumen desktop is already running."
else
  echo "Starting Lumen desktop..."
  nohup npm run desktop:dev >"$LOG_DIR/desktop-dev.out.log" 2>"$LOG_DIR/desktop-dev.err.log" &
  desktop_npm_pid=$!
  started+=("$desktop_npm_pid")
  # The first start builds the renderer before Electron opens; give it the longer budget.
  wait_for_desktop $(( $(date +%s) + TIMEOUT_SECONDS * 3 )) "$desktop_npm_pid"
fi

echo "Lumen desktop stack is ready."
echo "API: $API_URL"
echo "Desktop: Lumen"
echo "Logs: $LOG_DIR"
