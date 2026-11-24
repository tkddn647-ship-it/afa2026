#!/usr/bin/env bash
# Raspberry Pi → MediaMTX RTSP publish (서버 녹화용)
# 서버: CAMERA_RECORD_MODE=rtsp, CAMERA_RTSP_URL=rtsp://127.0.0.1:8554/cam
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ -f "$SCRIPT_DIR/stm_ingest.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/stm_ingest.env"
  set +a
fi

RTSP_URL="${RTSP_PUBLISH_URL:-rtsp://your.domain.example:8554/cam}"
WIDTH="${RTSP_WIDTH:-1280}"
HEIGHT="${RTSP_HEIGHT:-720}"
FPS="${RTSP_FPS:-30}"
BITRATE="${RTSP_BITRATE:-4000000}"
TRANSPORT="${RTSP_TRANSPORT:-tcp}"
# 카메라 장착이 좌우 반전일 때 기본 ON (끄려면 RTSP_HFLIP=0)
HFLIP="${RTSP_HFLIP:-1}"

RPICAM="$(command -v rpicam-vid || command -v libcamera-vid || true)"
FFMPEG="$(command -v ffmpeg || true)"

if [[ -z "$RPICAM" ]]; then
  echo "[fail] rpicam-vid / libcamera-vid 없음" >&2
  exit 1
fi
if [[ -z "$FFMPEG" ]]; then
  echo "[fail] ffmpeg 없음 (apt install ffmpeg)" >&2
  exit 1
fi

# HTTP JPEG camera.py / 이전 rpicam 잔여 프로세스와 충돌 방지
pkill -9 -f "rpicam-vid|libcamera-vid|rpicam-still|camera.py" 2>/dev/null || true
systemctl --user stop wireplumber pipewire pipewire-pulse 2>/dev/null || true
sleep 0.5

HFLIP_ARGS=()
if [[ "$HFLIP" == "1" || "$HFLIP" == "true" || "$HFLIP" == "yes" ]]; then
  HFLIP_ARGS=(--hflip)
fi

echo "[rtsp] publish → $RTSP_URL"
echo "[rtsp] ${WIDTH}x${HEIGHT}@${FPS} bitrate=${BITRATE} transport=${TRANSPORT} hflip=${HFLIP}"
echo "[hint] 서버에서 MediaMTX(:8554) + camera_server(:8012) 가 떠 있어야 합니다."
echo "[hint] 미리보기만 필요하면 camera.py 를 별도로 쓰지 말고 서버 RTSP preview 를 사용하세요."

while true; do
  set +e
  "$RPICAM" -t 0 --nopreview --inline \
    --width "$WIDTH" --height "$HEIGHT" --framerate "$FPS" \
    --bitrate "$BITRATE" \
    --codec h264 \
    "${HFLIP_ARGS[@]}" \
    -o - \
  | "$FFMPEG" -hide_banner -loglevel warning -fflags nobuffer -flags low_delay \
    -f h264 -i - \
    -c copy -an \
    -f rtsp -rtsp_transport "$TRANSPORT" \
    "$RTSP_URL"
  rc=$?
  set -e
  echo "[warn] RTSP 파이프 종료 rc=$rc — 3초 후 재연결"
  sleep 3
done
