#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu"
PY="/usr/bin/python3"
UVICORN="/home/ubuntu/.local/bin/uvicorn"
export PYTHONPATH="$ROOT"

is_listening() {
  ss -ltn 2>/dev/null | rg -q ":$1\\b"
}

start_if_down() {
  local port="$1"
  local name="$2"
  local dir="$3"
  local module="$4"
  local log="/tmp/${name}_server.log"

  if is_listening "$port"; then
    echo "[ok] $name ($port) already running"
    return
  fi

  echo "[start] $name on :$port"
  cd "$dir"
  nohup "$PY" "$UVICORN" "$module" --host 0.0.0.0 --port "$port" >>"$log" 2>&1 &
  sleep 2
  if is_listening "$port"; then
    echo "[ok] $name started"
  else
    echo "[fail] $name did not start — check $log" >&2
    exit 1
  fi
}

start_if_down 8000 logger "$ROOT/udp_logger" "app:app"
start_if_down 8011 realtime "$ROOT/udp_realtime" "realtime_server:app"
start_if_down 8012 camera "$ROOT/udp_realtime" "camera_server:app"

echo
ss -ltnp 2>/dev/null | rg ":(8000|8011|8012)\b" || true
