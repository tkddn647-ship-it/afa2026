#!/usr/bin/env bash
# systemd unit 설치 (sudo 필요). 현재 떠 있는 nohup 프로세스는 재시작 시 대체됩니다.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$ROOT/systemd"

sudo cp "$UNIT_DIR"/afa-mediamtx.service /etc/systemd/system/
sudo cp "$UNIT_DIR"/afa-ingest.service /etc/systemd/system/
sudo cp "$UNIT_DIR"/afa-realtime.service /etc/systemd/system/
sudo cp "$UNIT_DIR"/afa-camera.service /etc/systemd/system/
sudo cp "$UNIT_DIR"/afa-analysis.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable afa-mediamtx afa-ingest afa-realtime afa-camera afa-analysis

# 수동 기동 중인 uvicorn/mediamtx 정리 후 systemd로 전환
pkill -u afa -f 'uvicorn (app|realtime_server|camera_server|analysis):app' 2>/dev/null || true
pkill -u afa -f 'mediamtx' 2>/dev/null || true
sleep 1
sudo systemctl restart afa-mediamtx afa-ingest afa-realtime afa-camera afa-analysis
sleep 2
systemctl --no-pager --full status afa-mediamtx afa-ingest afa-realtime afa-camera afa-analysis | sed -n '1,100p'
echo
ss -ltn | grep -E ':(8000|8011|8012|8013|8554)\b' || true
echo "[ok] systemd services installed"
