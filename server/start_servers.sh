#!/usr/bin/env bash
# 8000=logging, 8011=realtime, 8012=camera, 8013=analysis, 8554=MediaMTX RTSP
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

if [[ -f "$ROOT/env.ingest" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/env.ingest"
  set +a
fi

AFA_PUBLIC_HOST="${AFA_PUBLIC_HOST:-your.domain.example}"
LOGGER_PORT="${LOGGER_PORT:-8000}"
REALTIME_PORT="${REALTIME_PORT:-8011}"
CAMERA_PORT="${CAMERA_PORT:-8012}"
ANALYSIS_PORT="${ANALYSIS_PORT:-8013}"
START_MEDIAMTX="${START_MEDIAMTX:-1}"

PY="$(command -v python3 || true)"
UVICORN="$(command -v uvicorn || true)"
if [[ -z "$UVICORN" && -x "$HOME/.local/bin/uvicorn" ]]; then
  UVICORN="$HOME/.local/bin/uvicorn"
fi
if [[ -z "$PY" || -z "$UVICORN" ]]; then
  echo "[fail] python3/uvicorn not found" >&2
  exit 1
fi

is_listening() {
  ss -ltn 2>/dev/null | rg -q ":$1\\b" || ss -ltn 2>/dev/null | grep -Eq ":$1[[:space:]]"
}

start_mediamtx() {
  local bin=""
  local cfg="$ROOT/udp_realtime/mediamtx/mediamtx.yml"
  local log="/tmp/mediamtx.log"

  if [[ "${START_MEDIAMTX}" != "1" ]]; then
    echo "[skip] MediaMTX (START_MEDIAMTX!=1)"
    return
  fi
  if is_listening 8554; then
    echo "[ok] mediamtx (8554) already running"
    return
  fi
  for candidate in \
    "$ROOT/udp_realtime/bin/mediamtx" \
    "$ROOT/udp_realtime/mediamtx/mediamtx" \
    "$(command -v mediamtx || true)"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      bin="$candidate"
      break
    fi
  done
  if [[ -z "$bin" ]]; then
    echo "[warn] mediamtx binary missing — run: $ROOT/install_mediamtx.sh" >&2
    return
  fi
  if [[ ! -f "$cfg" ]]; then
    echo "[warn] mediamtx.yml missing: $cfg" >&2
    return
  fi
  echo "[start] mediamtx on :8554"
  (
    cd "$(dirname "$cfg")"
    nohup "$bin" "$cfg" >>"$log" 2>&1 &
  )
  sleep 1
  if is_listening 8554; then
    echo "[ok] mediamtx started"
  else
    echo "[warn] mediamtx did not start — check $log" >&2
  fi
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

echo "[info] public host=$AFA_PUBLIC_HOST"
echo "[info] ports logger=$LOGGER_PORT realtime=$REALTIME_PORT camera=$CAMERA_PORT analysis=$ANALYSIS_PORT rtsp=8554"

start_mediamtx
start_if_down "$LOGGER_PORT" logger "$ROOT/udp_logger" "app:app"
start_if_down "$REALTIME_PORT" realtime "$ROOT/udp_realtime" "realtime_server:app"
start_if_down "$CAMERA_PORT" camera "$ROOT/udp_realtime" "camera_server:app"

# analysis: soft-fail 없이 동일하게 기동 (실패 시 exit)
if is_listening "$ANALYSIS_PORT"; then
  echo "[ok] analysis ($ANALYSIS_PORT) already running"
else
  echo "[start] analysis on :$ANALYSIS_PORT"
  (
    cd "$ROOT/analysis_server"
    ulimit -n 65536 2>/dev/null || true
    nohup "$PY" "$UVICORN" analysis:app --host 0.0.0.0 --port "$ANALYSIS_PORT" \
      --log-level warning --no-access-log >>/tmp/analysis_server.log 2>&1 &
  )
  sleep 2
  if is_listening "$ANALYSIS_PORT"; then
    echo "[ok] analysis started"
  else
    echo "[fail] analysis did not start — check /tmp/analysis_server.log" >&2
    exit 1
  fi
fi

echo
echo "=== listening ==="
ss -ltnp 2>/dev/null | rg ":(${LOGGER_PORT}|${REALTIME_PORT}|${CAMERA_PORT}|${ANALYSIS_PORT}|8554)\\b" \
  || ss -ltnp 2>/dev/null | grep -E ":(${LOGGER_PORT}|${REALTIME_PORT}|${CAMERA_PORT}|${ANALYSIS_PORT}|8554)[[:space:]]" \
  || true
echo
echo "UI:"
echo "  logging  http://${AFA_PUBLIC_HOST}:${LOGGER_PORT}"
echo "  realtime http://${AFA_PUBLIC_HOST}:${REALTIME_PORT}"
echo "  camera   http://${AFA_PUBLIC_HOST}:${CAMERA_PORT}"
echo "  analysis http://${AFA_PUBLIC_HOST}:${ANALYSIS_PORT}"
echo "RTSP publish target: rtsp://${AFA_PUBLIC_HOST}:8554/cam"
