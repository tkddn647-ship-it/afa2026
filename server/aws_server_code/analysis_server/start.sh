#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/analysis_server"
PY="/usr/bin/python3"
UVICORN="/home/ubuntu/.local/bin/uvicorn"
PORT="${PORT:-8011}"
LOG="/tmp/analysis_server.log"

export PYTHONPATH="/home/ubuntu:${PYTHONPATH:-}"
# Video range requests open many FDs; soft limit 1024 causes Errno 24.
ulimit -n 65536 2>/dev/null || true
if [[ -f /home/ubuntu/env.ingest ]]; then
  set -a
  # shellcheck disable=SC1091
  source /home/ubuntu/env.ingest
  set +a
fi
cd "$ROOT"

if ss -ltn 2>/dev/null | rg -q ":${PORT}\\b"; then
  echo "[ok] analysis server already listening on :$PORT"
  exit 0
fi

# 포트는 비었는데 uvicorn만 남은 경우(종료 대기 중) 정리
stale="$(pgrep -f "uvicorn analysis:app --host 0.0.0.0 --port ${PORT}" || true)"
if [ -n "$stale" ]; then
  echo "[cleanup] removing stale uvicorn on :$PORT"
  kill $stale 2>/dev/null || true
  sleep 1
  stale2="$(pgrep -f "uvicorn analysis:app --host 0.0.0.0 --port ${PORT}" || true)"
  if [ -n "$stale2" ]; then
    kill -9 $stale2 2>/dev/null || true
    sleep 1
  fi
fi

echo "[start] analysis server on :$PORT"
nohup "$PY" "$UVICORN" analysis:app --host 0.0.0.0 --port "$PORT" --log-level warning --no-access-log >>"$LOG" 2>&1 &
sleep 2
if ss -ltn 2>/dev/null | rg -q ":${PORT}\\b"; then
  echo "[ok] started — log: $LOG"
else
  echo "[fail] did not start — check $LOG" >&2
  tail -20 "$LOG" >&2 || true
  exit 1
fi
