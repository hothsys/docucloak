#!/usr/bin/env bash
# Start, stop, check DocuCloak.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$APP_DIR/app.conf"
APP_TITLE="${APP_TITLE:-$APP_NAME}"
PID_FILE="$APP_DIR/.server.pid"
PORT_FILE="$APP_DIR/.server.port"
CODE_FILE="$APP_DIR/.server.code"   # checksum of the app files the running server was started with
LOG_FILE="$APP_DIR/server.log"
DEFAULT_PORT="${PORT:-5903}"  # PORT comes from app.conf (sourced above)

running_pid() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid; pid="$(cat "$PID_FILE")"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then echo "$pid"; return 0; fi
  rm -f "$PID_FILE" "$PORT_FILE" "$CODE_FILE"; return 1
}

port_in_use() {
  python3 -c 'import socket,sys;sys.exit(socket.socket().connect_ex(("127.0.0.1",int(sys.argv[1])))!=0)' "$1"
}

open_browser() {
  if command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1 || true; else open "$1" 2>/dev/null || true; fi
}

code_checksum() {
  # Content, not modification time: AirDrop and copies keep the old date.
  # Include the launcher and every backend module, since all are imported at startup.
  python3 - "$APP_DIR" <<'PY' | cksum
import pathlib, sys
root = pathlib.Path(sys.argv[1])
for path in [root / 'app.py', *sorted((root / 'docucloak').rglob('*.py'))]:
    sys.stdout.buffer.write(path.read_bytes())
PY
}

start() {
  local port="${1:-$DEFAULT_PORT}"
  local pid
  if pid="$(running_pid)"; then
    if [[ "$(cat "$CODE_FILE" 2>/dev/null)" == "$(code_checksum)" ]]; then
      echo "$APP_TITLE running (PID $pid) at http://localhost:$(cat "$PORT_FILE")"; open_browser "http://localhost:$(cat "$PORT_FILE")"; return 0
    fi
    echo "$APP_TITLE was updated since it started; restarting it."
    stop
  fi
  if ! command -v tesseract >/dev/null 2>&1 && [[ ! -x /opt/homebrew/bin/tesseract && ! -x /usr/local/bin/tesseract ]]; then
    echo "Warning: Tesseract not found; scanned PDFs and images can't be redacted (brew install tesseract)." >&2
  fi
  code_checksum > "$CODE_FILE"
  nohup python3 "$APP_DIR/app.py" >> "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  # Wait briefly for port to bind
  for i in {1..30}; do
    sleep 0.2
    if [[ -f "$PORT_FILE" ]]; then break; fi
    if python3 -c 'import socket,sys;sys.exit(socket.socket().connect_ex(("127.0.0.1",'$port'))==0)' 2>/dev/null; then echo "$port" > "$PORT_FILE"; break; fi
  done
  if [[ ! -f "$PORT_FILE" ]]; then echo "$port" > "$PORT_FILE"; fi
  echo "$APP_TITLE started at http://localhost:$port (PID $(cat "$PID_FILE"))"
  open_browser "http://localhost:$port"
}

stop() {
  local pid
  if pid="$(running_pid)"; then kill "$pid" 2>/dev/null || true; sleep 1; fi
  rm -f "$PID_FILE" "$PORT_FILE" "$CODE_FILE"
  echo "$APP_TITLE stopped."
}

status() {
  if pid="$(running_pid)"; then echo "Running PID $pid at http://localhost:$(cat "$PORT_FILE" 2>/dev/null || echo '?')"; else echo "Not running."; fi
}

case "${1:-start}" in
  start) start "${2:-}" ;;
  stop) stop ;;
  restart) stop; sleep 0.5; start "${2:-}" ;;
  status) status ;;
  logs) tail -f "$LOG_FILE" ;;
  *) echo "Usage: $0 {start|stop|restart|status|logs} [port]"; exit 1 ;;
esac
