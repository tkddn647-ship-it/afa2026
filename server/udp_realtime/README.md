# udp_realtime

기존 `/home/ubuntu/udp_logger/app.py`와 분리된 독립 실시간 서버입니다.

실행:

```bash
cd /home/ubuntu/udp_realtime
uvicorn realtime_server:app --host 0.0.0.0 --port 8011
```

기존 로깅 서버에서 이 서버로 포워딩하려면:

```bash
cd /home/ubuntu/udp_logger
FORWARD_URL=http://127.0.0.1:8011/api/live/ingest uvicorn app:app --host 0.0.0.0 --port 8010
```
