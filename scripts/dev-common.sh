# Shared helpers for the macOS/Linux dev scripts (dev-start.sh, restart-dev.sh). Sourced, not run.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO_ROOT/storage/logs"
API_URL="http://127.0.0.1:8001"
API_HEALTH_URL="$API_URL/health"
DEV_PORTS=(8001 3210 3211 3212 3213 3214 3215 3216 3217 3218 3219)

mkdir -p "$LOG_DIR"

# The repo's virtualenv goes first on PATH so `python` (npm run api:dev, the Electron host's backend
# launch, mode_check) is the project's interpreter, not the system one.
if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  export PATH="$REPO_ROOT/.venv/bin:$PATH"
fi
if ! command -v python >/dev/null 2>&1; then
  echo "No 'python' on PATH. Create the venv: python3.12 -m venv .venv && .venv/bin/pip install -r apps/api/requirements.txt" >&2
  exit 1
fi

http_ok() {
  curl -fsS --max-time 2 -o /dev/null "$1" 2>/dev/null
}

# Wait for an HTTP 200 until the deadline (epoch seconds); fail early if the given pid exits.
wait_for_http_ok() {
  local name="$1" url="$2" deadline="$3" pid="${4:-}"
  while (( $(date +%s) < deadline )); do
    if http_ok "$url"; then
      echo "$name is healthy at $url"
      return 0
    fi
    if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
      echo "$name exited before $url returned HTTP 200. Check logs in $LOG_DIR." >&2
      return 1
    fi
    sleep 1
  done
  echo "$name did not return HTTP 200 at $url in time. Check logs in $LOG_DIR." >&2
  return 1
}

# The Electron host of this checkout (apps/desktop), if one is running: started from apps/desktop
# (`electron .`) or with that folder as its argument (launched through `open` on macOS).
desktop_pid() {
  { pgrep -f "Electron.* \.$|Electron $REPO_ROOT/apps/desktop" 2>/dev/null || true; } | while read -r pid; do
    if ps -o command= -p "$pid" 2>/dev/null | grep -qF "Electron $REPO_ROOT/apps/desktop" \
      || [[ "$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')" == "$REPO_ROOT/apps/desktop" ]]; then
      echo "$pid"
      break
    fi
  done
  return 0
}

# macOS charges Accessibility and Screen Recording to the app that launched a process. Launched through
# LaunchServices (`open`), the dev Electron host is that app for itself and for the API it starts, so those
# permissions are granted to Lumen's dev host rather than to Terminal or the editor.
open_desktop_host_macos() {
  open -n -g "$REPO_ROOT/node_modules/electron/dist/Electron.app" \
    --env LUMEN_PYTHON="$REPO_ROOT/.venv/bin/python" \
    --args "$REPO_ROOT/apps/desktop"
}

wait_for_desktop() {
  local deadline="$1" pid="${2:-}"
  while (( $(date +%s) < deadline )); do
    local found
    found="$(desktop_pid)"
    if [[ -n "$found" ]]; then
      echo "Lumen desktop is ready (pid=$found)."
      return 0
    fi
    if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
      echo "Lumen desktop exited before its window opened. Check logs in $LOG_DIR." >&2
      return 1
    fi
    sleep 1
  done
  echo "Lumen desktop did not open in time. Check logs in $LOG_DIR." >&2
  return 1
}
