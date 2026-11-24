# AFA Realtime / Camera

## 포트

| 서비스 | 포트 | URL |
|--------|------|-----|
| Logging | 8000 | http://your.domain.example:8000 |
| Realtime | 8011 | http://your.domain.example:8011 |
| Camera | 8012 | http://your.domain.example:8012 |
| Analysis | 8013 | http://your.domain.example:8013 |
| MediaMTX RTSP | 8554 | rtsp://your.domain.example:8554/cam |

## 기동 (서버)

```bash
cd server
./install_ffmpeg.sh      # 최초 1회
./install_mediamtx.sh    # 최초 1회 (RTSP 중계)
./start_servers.sh       # logger+realtime+camera+analysis+mediamtx
```

설정은 `server/env.ingest` (`AFA_PUBLIC_HOST=your.domain.example`).

## 카메라 녹화 (RTSP)

서버 기본값: `CAMERA_RECORD_MODE=rtsp`  
Pi에서 HTTP JPEG 대신 RTSP 송출:

```bash
# Raspberry Pi
cd "device/raspberry pi 5"
cp stm_ingest.env.example stm_ingest.env   # 필요 시 수정
./rtsp_publish.sh
```

녹화는 camera_server가 `rtsp://127.0.0.1:8554/cam` 을 ffmpeg `-c copy` 로 pull 합니다.
HTTP `camera.py` 는 선택적 미리보기용이며, 녹화 경로가 아닙니다.

## 상호 연동

- Logger → Realtime: `FORWARD_URL=http://127.0.0.1:8011/api/live/ingest`
- Logger → Camera 녹화 연동: `CAMERA_INTERNAL_URL=http://127.0.0.1:8012`
- Camera HUD 텔레메트리: `LOGGER_SNAPSHOT_URL=http://127.0.0.1:8011/api/live/snapshot`
- UI 네비: 로깅 / 실시간 / 카메라 / 분석
