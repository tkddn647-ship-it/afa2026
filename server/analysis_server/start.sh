#!/usr/bin/env bash
# analysis server — default port 8013
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ROOT="$SCRIPT_DIR"
PY="$(command -v python3 || echo /usr/bin/python3)"
UVICORN="$(command -v uvicorn || true)"
if [[ -z "$UVICORN" && -x "$HOME/.local/bin/uvicorn" ]]; then
  UVICORN="$HOME/.local/bin/uvicorn"
fi
if [[ -z "$UVICORN" ]]; then
  echo "[fail] uvicorn not found" >&2
  exit 1
fi

export PYTHONPATH="${SERVER_ROOT}:${PYTHONPATH:-}"
# Video range requests open many FDs; soft limit 1024 causes Errno 24.
ulimit -n 65536 2>/dev/null || true
if [[ -f "$SERVER_ROOT/env.ingest" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$SERVER_ROOT/env.ingest"
  set +a
fi

PORT="${PORT:-${ANALYSIS_PORT:-8013}}"
LOG="/tmp/analysis_server.log"
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
