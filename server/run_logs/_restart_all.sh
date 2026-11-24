#!/usr/bin/env bash
set -u
ROOT=/home/afa/다운로드/afa2026-main/server
PY="$ROOT/.venv/bin/python3"
LOGDIR="$ROOT/run_logs"
mkdir -p "$LOGDIR"

kill_match() {
  local pat="$1"
  local p
  for p in $(pgrep -f "$pat" || true); do
    kill "$p" 2>/dev/null || true
  done
  sleep 1
  for p in $(pgrep -f "$pat" || true); do
    kill -9 "$p" 2>/dev/null || true
  done
}

echo "[$(date -Is)] killing uvicorn services..."
kill_match '[u]vicorn app:app'
kill_match '[u]vicorn realtime_server:app'
kill_match '[u]vicorn camera_server:app'
kill_match '[u]vicorn analysis:app'
sleep 1

if ! ss -tln | grep -q ':8554'; then
  echo "[start] mediamtx"
  nohup "$ROOT/udp_realtime/bin/mediamtx" "$ROOT/udp_realtime/mediamtx/mediamtx.yml" \
    >"$LOGDIR/mediamtx.log" 2>&1 &
  sleep 1
fi

set -a
# shellcheck disable=SC1091
source "$ROOT/env.ingest"
set +a

echo "[start] logger"
cd "$ROOT/udp_logger"
nohup "$PY" -m uvicorn app:app --host 0.0.0.0 --port 8000 >"$LOGDIR/ingest.log" 2>&1 &
echo "ingest=$!"

echo "[start] realtime"
cd "$ROOT/udp_realtime"
nohup "$PY" -m uvicorn realtime_server:app --host 0.0.0.0 --port 8011 >"$LOGDIR/realtime.log" 2>&1 &
echo "realtime=$!"

echo "[start] camera"
nohup "$PY" -m uvicorn camera_server:app --host 0.0.0.0 --port 8012 >"$LOGDIR/camera.log" 2>&1 &
echo "camera=$!"

echo "[start] analysis"
cd "$ROOT/analysis_server"
nohup "$PY" -m uvicorn analysis:app --host 0.0.0.0 --port 8013 --log-level warning --no-access-log \
  >"$LOGDIR/analysis.log" 2>&1 &
echo "analysis=$!"

for i in $(seq 1 60); do
  n=0
  ss -tln | grep -q ':8000' && n=$((n+1))
  ss -tln | grep -q ':8011' && n=$((n+1))
  ss -tln | grep -q ':8012' && n=$((n+1))
  ss -tln | grep -q ':8013' && n=$((n+1))
  ss -tln | grep -q ':8554' && n=$((n+1))
  echo "wait ${i}s listening=$n/5"
  if [ "$n" -ge 5 ]; then
    break
  fi
  sleep 1
done

echo '=== PORTS ==='
ss -tlnp | grep -E ':8000|:8011|:8012|:8013|:8554' || true
echo '=== HEALTH ==='
curl -s --max-time 3 http://127.0.0.1:8000/api/stats | python3 -c "import sys,json;d=json.load(sys.stdin); print('logger',d.get('hz_1s'),'pi',(d.get('device_hz') or {}).get('raspberry_pi',{}).get('hz_1s'))" || echo logger_fail
curl -s --max-time 3 http://127.0.0.1:8011/api/stats | python3 -c "import sys,json;d=json.load(sys.stdin); print('realtime live',d.get('live'),'hz',d.get('hz_1s'))" || echo realtime_fail
curl -s --max-time 3 http://127.0.0.1:8012/api/recording/status | python3 -c "import sys,json;d=json.load(sys.stdin); print('camera ok',d.get('ok'))" || echo camera_fail
curl -s -o /dev/null -w 'analysis http %{http_code}\n' --max-time 5 http://127.0.0.1:8013/ || echo analysis_fail
ps -C python3,mediamtx -o pid=,etime=,cmd= | awk '/uvicorn|mediamtx/'
