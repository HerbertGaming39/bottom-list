#!/usr/bin/env bash
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
mkdir -p .pids logs

PY_SYS="${PYTHON:-python3}"
VENV_DIR="${VENV_DIR:-venv}"
AUTO_INSTALL="${AUTO_INSTALL:-1}"
AUTO_UPDATE="${AUTO_UPDATE:-0}"

if [ ! -d "$VENV_DIR" ]; then
  "$PY_SYS" -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

PY="python"

if [ "$AUTO_INSTALL" = "1" ]; then
  "$PY" -m pip install --upgrade pip
  if [ "$AUTO_UPDATE" = "1" ]; then
    "$PY" -m pip install --upgrade -r requirements.txt
  else
    "$PY" -m pip install -r requirements.txt
  fi
fi

export COOKIE_SECURE=1

start_proc() {
  local name="$1"
  local file="$2"
  local log="$3"
  local pidfile="$4"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "$file already running (pid $(cat "$pidfile"))"
    return 0
  fi
  rm -f "$pidfile"
  nohup "$PY" -u "$file" > "$log" 2>&1 &
  echo $! > "$pidfile"
  sleep 1
  if kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "$name started (pid $(cat "$pidfile"))"
    return 0
  fi
  echo "$name failed to start. Last logs:"
  tail -n 60 "$log" || true
  return 1
}

start_proc "web" "app.py" "logs/app.out" ".pids/app.pid"
start_proc "bot" "discord_bot.py" "logs/bot.out" ".pids/bot.pid"
echo "launched"
