import asyncio
import base64
import binascii
import os
import queue
import threading
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from fastapi import FastAPI, Request
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse

app = FastAPI()
BASE_DIR = Path(__file__).resolve().parent
RECORDINGS_DIR = BASE_DIR / "camera_recordings"
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

STREAM_IDLE_SLEEP_SEC = float(os.getenv("CAMERA_STREAM_IDLE_SLEEP_SEC", "0.01"))
STREAM_HEARTBEAT_SEC = float(os.getenv("CAMERA_STREAM_HEARTBEAT_SEC", "1.0"))
DEFAULT_RECORD_FPS = float(os.getenv("CAMERA_RECORD_FPS", "24"))
PUBLIC_BASE_URL = os.getenv("CAMERA_PUBLIC_BASE_URL", "http://3.39.188.80:8012").rstrip("/")
MAX_RECORDING_BYTES = int(os.getenv("CAMERA_MAX_RECORDING_BYTES", str(1024 * 1024 * 1024)))

camera_state: dict[str, Any] = {
    "mime": "image/jpeg",
    "width": 0,
    "height": 0,
    "seq": 0,
    "client_seq": 0,
    "client_session": "",
    "source": "",
    "updated_at": "",
    "bytes": 0,
    "frame_url": "/api/camera/latest.jpg",
    "stream_url": "/api/camera/stream.mjpg",
}
camera_frame_bytes = b""
camera_lock = threading.Lock()
last_camera_log_at = 0.0

recording_lock = threading.Lock()
recording_writer: Any = None
recording_queue: queue.Queue = queue.Queue(maxsize=int(os.getenv("CAMERA_RECORD_QUEUE", "120")))
recording_stop_event = threading.Event()
recording_state: dict[str, Any] = {
    "active": False,
    "session_id": "",
    "filename": "",
    "mode": "",
    "mime": "",
    "frames_written": 0,
    "bytes_written": 0,
    "fps": DEFAULT_RECORD_FPS,
    "width": 0,
    "height": 0,
    "started_at": "",
    "updated_at": "",
    "message": "",
}
recording_worker_started = False


class MjpegFileWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fp = path.open("wb")

    def write(self, jpeg_bytes: bytes) -> None:
        self.fp.write(
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            + f"Content-Length: {len(jpeg_bytes)}\r\n\r\n".encode("ascii")
            + jpeg_bytes
            + b"\r\n"
        )

    def release(self) -> None:
        if self.fp.closed:
            return
        self.fp.flush()
        self.fp.close()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def recording_storage_bytes() -> int:
    return sum(path.stat().st_size for path in RECORDINGS_DIR.glob("*.mjpg") if path.is_file())


def cleanup_recordings(max_bytes: int = MAX_RECORDING_BYTES) -> list[str]:
    deleted: list[str] = []
    files = sorted(RECORDINGS_DIR.glob("*.mjpg"), key=lambda path: path.stat().st_mtime)
    total = sum(path.stat().st_size for path in files if path.is_file())
    for path in files:
        if total <= max_bytes:
            break
        if not path.is_file():
            continue
        size = path.stat().st_size
        path.unlink()
        total -= size
        deleted.append(path.name)
    return deleted


def list_recordings(limit: int = 20) -> list[dict[str, Any]]:
    files = sorted(RECORDINGS_DIR.glob("*.mjpg"), key=lambda path: path.stat().st_mtime, reverse=True)
    items: list[dict[str, Any]] = []
    for path in files[:limit]:
        stat = path.stat()
        relative_url = f"/api/recordings/{path.name}"
        items.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "url": relative_url,
                "download_url": f"{PUBLIC_BASE_URL}{relative_url}",
                "download_delete_url": f"{PUBLIC_BASE_URL}{relative_url}?delete_after=1",
            }
        )
    return items


def latest_camera_bytes() -> tuple[bytes, dict[str, Any]]:
    with camera_lock:
        return camera_frame_bytes, dict(camera_state)


def _unique_recording_path(filename: str) -> Path:
    safe_name = Path(filename).name
    if not safe_name.endswith(".mjpg"):
        safe_name = f"{Path(safe_name).stem}.mjpg"

    target = RECORDINGS_DIR / safe_name
    counter = 1
    while target.exists():
        target = RECORDINGS_DIR / f"{Path(safe_name).stem}-{counter}.mjpg"
        counter += 1
    return target


def _record_frame(frame_bytes: bytes, width: int, height: int) -> None:
    global recording_writer
    with recording_lock:
        if not recording_state["active"] or recording_writer is None:
            return

    with recording_lock:
        if recording_state["active"] and recording_writer is not None:
            recording_writer.write(frame_bytes)
            recording_state["frames_written"] = int(recording_state["frames_written"]) + 1
            recording_state["bytes_written"] = int(recording_state["bytes_written"]) + len(frame_bytes)
            recording_state["updated_at"] = datetime.now(timezone.utc).isoformat()


def put_recording_frame(frame_bytes: bytes, width: int, height: int) -> None:
    if not recording_state["active"]:
        return

    item = (frame_bytes, width, height)
    try:
        recording_queue.put_nowait(item)
    except queue.Full:
        try:
            recording_queue.get_nowait()
            recording_queue.task_done()
        except queue.Empty:
            pass
        try:
            recording_queue.put_nowait(item)
        except queue.Full:
            pass


def recording_worker() -> None:
    while not recording_stop_event.is_set():
        try:
            frame_bytes, width, height = recording_queue.get(timeout=0.2)
        except queue.Empty:
            continue
        try:
            _record_frame(frame_bytes, width, height)
        finally:
            recording_queue.task_done()


def ensure_recording_worker() -> None:
    global recording_worker_started
    if recording_worker_started:
        return
    thread = threading.Thread(target=recording_worker, daemon=True)
    thread.start()
    recording_worker_started = True


def stop_recording(message: str = "") -> None:
    global recording_writer
    with recording_lock:
        writer = recording_writer
        mode = str(recording_state.get("mode") or "")
        recording_writer = None
        recording_state["active"] = False
        recording_state["message"] = message
        recording_state["updated_at"] = datetime.now(timezone.utc).isoformat()

    if writer is not None:
        writer.release()


def update_camera_state(
    frame_bytes: bytes,
    *,
    mime: str = "image/jpeg",
    width: int = 0,
    height: int = 0,
    source: str = "raspberry-pi",
    client_seq: int = 0,
    client_session: str = "",
) -> tuple[int, bool]:
    global camera_frame_bytes, last_camera_log_at
    now = datetime.now(timezone.utc).isoformat()
    safe_mime = mime or "image/jpeg"
    safe_source = source or "raspberry-pi"

    with camera_lock:
        current_client_seq = int(camera_state.get("client_seq", 0) or 0)
        current_client_session = str(camera_state.get("client_session", "") or "")
        same_session = bool(client_session) and client_session == current_client_session
        if same_session and client_seq > 0 and current_client_seq > 0 and client_seq <= current_client_seq:
            return int(camera_state.get("seq", 0) or 0), False

        camera_frame_bytes = frame_bytes
        camera_state["mime"] = safe_mime
        camera_state["width"] = width
        camera_state["height"] = height
        camera_state["source"] = safe_source
        camera_state["seq"] = int(camera_state.get("seq", 0)) + 1
        if client_seq > 0:
            camera_state["client_seq"] = client_seq
        if client_session:
            camera_state["client_session"] = client_session
        camera_state["updated_at"] = now
        camera_state["bytes"] = len(frame_bytes)
        seq = int(camera_state["seq"])

    if recording_state["active"]:
        put_recording_frame(frame_bytes, width, height)

    log_now = monotonic()
    if log_now - last_camera_log_at >= 1.0:
        last_camera_log_at = log_now
        print(f"[camera] seq={seq} client_seq={client_seq} bytes={len(frame_bytes)} source={safe_source}")

    return seq, True


@app.get("/")
def home() -> HTMLResponse:
    return HTMLResponse(
        """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Camera Server</title>
  <style>
    :root { color-scheme: dark; --bg:#070b12; --panel:#111827; --panel-2:#0b111c; --line:#26364f; --text:#f7f9fd; --muted:#9bb7d9; --ok:#4ade80; --warn:#fbbf24; }
    * { box-sizing: border-box; }
    body { margin: 0; background: radial-gradient(circle at top left, #13233b 0, var(--bg) 46%); color: var(--text); font-family: Arial, sans-serif; }
    main { min-height: 100vh; padding: 22px; }
    .wrap { width: min(1040px, 100%); margin: 0 auto; display: grid; gap: 14px; }
    header { display: flex; justify-content: space-between; align-items: end; gap: 14px; }
    h1 { margin: 0; font-size: 34px; line-height: 1; }
    .sub { margin-top: 8px; color: var(--muted); font-size: 14px; }
    .stream-card, .bar, .recordings { background: linear-gradient(180deg, var(--panel), var(--panel-2)); border: 1px solid var(--line); border-radius: 8px; }
    .stream-card { padding: 14px; }
    img { width: 100%; max-height: 58vh; aspect-ratio: 4 / 3; object-fit: contain; background: #000; display: block; border-radius: 6px; }
    .bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; padding: 14px; }
    .recordings { padding: 14px; font-size: 14px; display: grid; gap: 10px; }
    .file-row { display: flex; gap: 10px; align-items: center; justify-content: space-between; flex-wrap: wrap; padding: 10px; border: 1px solid #24344c; border-radius: 6px; background: #090f18; }
    .file-name { color: #eaf4ff; font-weight: 700; }
    .file-meta { color: #9bb7d9; font-size: 12px; margin-top: 2px; }
    .file-actions { display: flex; gap: 8px; flex-wrap: wrap; }
    a.btnlink { text-decoration: none; display: inline-flex; align-items: center; }
    button, a.btnlink { min-height: 38px; padding: 0 14px; border: 1px solid #2f6c9b; border-radius: 6px; background: #0e2b46; color: #e7f7ff; font-weight: 700; cursor: pointer; }
    button:disabled { opacity: .45; cursor: not-allowed; }
    button.stop, a.btnlink.stop { border-color: #8c2638; background: #3a101a; color: #ffd8df; }
    code { color: #b8d7ff; overflow-wrap: anywhere; }
    #status { font-size: 13px; color: #c9ced6; overflow-wrap: anywhere; }
    .ok { color: var(--ok); }
    .warn { color: var(--warn); }
  </style>
</head>
<body>
  <main>
    <div class="wrap">
      <header>
        <div>
          <h1>Camera Server</h1>
          <div class="sub">8012 카메라 스트림 및 서버 측 MJPG 녹화</div>
        </div>
        <code>/api/camera/stream.mjpg</code>
      </header>
      <div class="stream-card"><img id="streamImage" src="/api/camera/stream.mjpg" alt="camera stream"></div>
      <div class="bar">
        <button id="startButton" type="button">녹화 시작</button>
        <button id="stopButton" class="stop" type="button" disabled>녹화 중지</button>
        <span id="status">페이지 로딩 중</span>
      </div>
      <div class="recordings" id="recordings">저장된 녹화본이 없습니다.</div>
    </div>
  </main>
  <script>
    // === 서버 저장 방식 ===
    // 녹화 시작/중지는 서버 API(/api/recording/start, /stop)를 호출한다.
    // 실제 영상 저장은 서버가 라즈베리파이 원본 프레임을 받아 camera_recordings/*.mjpg 로 한다.
    // 브라우저는 "버튼을 누르고 상태를 표시"만 한다. (새로고침해도 녹화는 서버에서 계속됨)

    let polling = false;

    function setStatus(message) {
      document.getElementById('status').textContent = message;
    }

    function formatMb(size) { return (Number(size || 0) / 1024 / 1024).toFixed(1) + ' MB'; }

    async function postJson(url) {
      const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      let data = {};
      try { data = await res.json(); } catch (e) {}
      return { ok: res.ok, data };
    }

    function renderRecordings(files) {
      const box = document.getElementById('recordings');
      if (!files || !files.length) {
        box.textContent = '저장된 녹화본이 없습니다.';
        return;
      }
      box.innerHTML = files.map((f) =>
        '<div class="file-row">' +
          '<div><div class="file-name">' + f.name + '</div>' +
          '<div class="file-meta">' + formatMb(f.size) + ' · ' + (f.updated_at || '') + '</div></div>' +
          '<div class="file-actions">' +
            '<a class="btnlink" href="' + f.download_url + '" target="_blank" rel="noreferrer">Download</a>' +
            '<button type="button" class="stop" data-delete-name="' + f.name + '">Delete</button>' +
          '</div>' +
        '</div>'
      ).join('');
      box.querySelectorAll('[data-delete-name]').forEach((button) => {
        button.onclick = () => deleteRecording(button.dataset.deleteName);
      });
    }

    async function deleteRecording(name) {
      try {
        const res = await fetch('/api/recordings/' + encodeURIComponent(name), { method: 'DELETE' });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.message || '삭제 실패');
        setStatus(name + ' 삭제됨');
        renderRecordings(data.recent_files || []);
      } catch (err) {
        setStatus('삭제 실패: ' + (err.message || err));
      }
    }

    async function refreshStatus() {
      try {
        const res = await fetch('/api/recording/status', { cache: 'no-store' });
        const data = await res.json();
        const active = Boolean(data.active);
        document.getElementById('startButton').disabled = active;
        document.getElementById('stopButton').disabled = !active;
        if (active) {
          setStatus('서버 녹화 중... ' + (data.filename || '') + ' / ' + formatMb(data.bytes_written) + ' / ' + (data.frames_written || 0) + ' frames');
        } else if (!polling) {
          setStatus('대기 중 (녹화 시작을 누르세요)');
        }
        renderRecordings(data.recent_files || []);
      } catch (err) {
        setStatus('상태 조회 실패: ' + (err.message || err));
      }
    }

    async function startRecording() {
      try {
        setStatus('녹화 시작 요청 중...');
        document.getElementById('startButton').disabled = true;
        const { ok, data } = await postJson('/api/recording/start');
        if (!ok || !data.ok) {
          // 서버가 거부한 경우 (예: 아직 카메라 프레임이 안 들어옴)
          document.getElementById('startButton').disabled = false;
          setStatus('녹화 시작 실패: ' + (data.message || '알 수 없는 오류'));
          return;
        }
        setStatus('서버 녹화를 시작했습니다: ' + (data.filename || ''));
        await refreshStatus();
      } catch (err) {
        document.getElementById('startButton').disabled = false;
        setStatus('녹화 시작 실패: ' + (err.message || err));
      }
    }

    async function stopRecording() {
      try {
        setStatus('녹화 중지 요청 중...');
        document.getElementById('stopButton').disabled = true;
        const { ok, data } = await postJson('/api/recording/stop');
        if (!ok || !data.ok) {
          setStatus('녹화 중지 실패: ' + (data.message || '진행 중인 녹화가 없습니다.'));
          await refreshStatus();
          return;
        }
        const rec = data.recording || {};
        setStatus('녹화 종료. ' + (rec.filename || '') + ' / ' + (rec.frames_written || 0) + ' frames 저장됨');
        renderRecordings(data.recent_files || []);
        await refreshStatus();
      } catch (err) {
        setStatus('녹화 중지 실패: ' + (err.message || err));
        await refreshStatus();
      }
    }

    document.getElementById('startButton').onclick = startRecording;
    document.getElementById('stopButton').onclick = stopRecording;

    // 시작 시 1회 + 이후 2초마다 서버 상태/파일목록 동기화
    refreshStatus();
    setInterval(() => { polling = true; refreshStatus(); }, 2000);
  </script>
</body>
</html>
        """.strip()
    )

@app.post("/api/camera/frame")
async def camera_frame(request: Request) -> Any:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type.startswith("image/") or content_type == "application/octet-stream":
        frame_bytes = await request.body()
        if not frame_bytes:
            return {"ok": False, "message": "frame is required"}

        update_camera_state(
            frame_bytes,
            mime=content_type if content_type.startswith("image/") else request.headers.get("x-frame-mime", "image/jpeg"),
            width=_safe_int(request.headers.get("x-frame-width")),
            height=_safe_int(request.headers.get("x-frame-height")),
            source=request.headers.get("x-device") or request.headers.get("x-source") or "raspberry-pi",
            client_seq=_safe_int(request.headers.get("x-frame-seq")),
            client_session=str(request.headers.get("x-frame-session") or ""),
        )
        return Response(status_code=204)

    body = await request.json()
    if not isinstance(body, dict):
        return {"ok": False, "message": "invalid body"}

    frame = str(body.get("frame", "")).strip()
    if not frame:
        return {"ok": False, "message": "frame is required"}

    try:
        frame_bytes = base64.b64decode(frame, validate=True)
    except (binascii.Error, ValueError):
        return {"ok": False, "message": "invalid base64 frame"}

    update_camera_state(
        frame_bytes,
        mime=str(body.get("mime", "image/jpeg") or "image/jpeg"),
        width=_safe_int(body.get("width")),
        height=_safe_int(body.get("height")),
        source=str(body.get("source", "") or body.get("device", "") or "raspberry-pi"),
        client_seq=_safe_int(body.get("seq")),
        client_session=str(body.get("session") or ""),
    )
    return Response(status_code=204)


@app.get("/api/camera/latest")
def camera_latest() -> dict[str, Any]:
    _, snapshot = latest_camera_bytes()
    return {"ok": True, **snapshot}


@app.get("/api/camera/latest.jpg")
def camera_latest_jpeg() -> Response:
    frame_bytes, snapshot = latest_camera_bytes()
    if not frame_bytes:
        return Response(status_code=204)

    return Response(
        content=frame_bytes,
        media_type=str(snapshot.get("mime") or "image/jpeg"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "X-Camera-Seq": str(snapshot.get("seq", 0)),
            "X-Camera-Updated-At": str(snapshot.get("updated_at", "")),
            "X-Camera-Source": str(snapshot.get("source", "")),
        },
    )


@app.get("/api/camera/stream.mjpg")
async def camera_stream_mjpeg():
    boundary = "frame"

    async def generate():
        last_seq = -1
        last_heartbeat = monotonic()
        while True:
            frame_bytes, snapshot = latest_camera_bytes()
            seq = int(snapshot.get("seq", 0) or 0)
            if frame_bytes and seq != last_seq:
                last_seq = seq
                last_heartbeat = monotonic()
                mime = str(snapshot.get("mime") or "image/jpeg")
                yield (
                    f"--{boundary}\r\n"
                    f"Content-Type: {mime}\r\n"
                    f"Content-Length: {len(frame_bytes)}\r\n"
                    f"X-Camera-Seq: {seq}\r\n\r\n"
                ).encode("ascii") + frame_bytes + b"\r\n"
                continue

            if monotonic() - last_heartbeat >= STREAM_HEARTBEAT_SEC:
                last_heartbeat = monotonic()
                yield f"--{boundary}\r\n\r\n".encode("ascii")

            await asyncio.sleep(max(STREAM_IDLE_SLEEP_SEC, 0.001))

    return StreamingResponse(
        generate(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/api/recording/status")
def recording_status() -> dict[str, Any]:
    with recording_lock:
        return {
            "ok": True,
            **recording_state,
            "recent_files": list_recordings(),
            "storage_bytes": recording_storage_bytes(),
            "max_storage_bytes": MAX_RECORDING_BYTES,
        }


@app.post("/api/recording/start")
async def recording_start(request: Request) -> dict[str, Any]:
    global recording_writer
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    with recording_lock:
        if recording_state["active"]:
            return {"ok": False, "message": "recording already active", **recording_state}

    frame_bytes, snapshot = latest_camera_bytes()
    if not frame_bytes:
        return {"ok": False, "message": "no camera frame has been received yet"}

    width = _safe_int(body.get("width"), _safe_int(snapshot.get("width")))
    height = _safe_int(body.get("height"), _safe_int(snapshot.get("height")))
    fps = max(1.0, min(60.0, _safe_float(body.get("fps"), DEFAULT_RECORD_FPS)))
    started_at = datetime.now(timezone.utc)
    session_id = started_at.strftime("%Y%m%dT%H%M%SZ")

    if width <= 0 or height <= 0:
        return {"ok": False, "message": "?꾨젅???ш린瑜??꾩쭅 ?????놁뼱 ?뱁솕瑜??쒖옉?????놁뒿?덈떎."}

    target = _unique_recording_path(str(body.get("filename") or f"camera-{session_id}.mjpg"))
    writer = MjpegFileWriter(target)

    while not recording_queue.empty():
        try:
            recording_queue.get_nowait()
            recording_queue.task_done()
        except queue.Empty:
            break

    with recording_lock:
        recording_writer = writer
        recording_state.update(
            {
                "active": True,
                "session_id": session_id,
                "filename": target.name,
                "mode": "mjpg",
                "mime": "multipart/x-mixed-replace; boundary=frame",
                "frames_written": 0,
                "bytes_written": 0,
                "fps": fps,
                "width": width,
                "height": height,
                "started_at": started_at.isoformat(),
                "updated_at": started_at.isoformat(),
                "message": "?뱁솕瑜??쒖옉?덉뒿?덈떎.",
            }
        )

    ensure_recording_worker()
    put_recording_frame(frame_bytes, width, height)
    print(f"[recording] started file={target} size={width}x{height} fps={fps:g}")
    return {"ok": True, **recording_state, "download_url": f"{PUBLIC_BASE_URL}/api/recordings/{target.name}"}


@app.post("/api/recording/stop")
def recording_stop() -> dict[str, Any]:
    with recording_lock:
        if not recording_state["active"]:
            return {"ok": False, "message": "recording is not active", "recent_files": list_recordings()}
        filename = str(recording_state["filename"] or "")
        finished = dict(recording_state)

    deadline = monotonic() + 1.0
    while not recording_queue.empty() and monotonic() < deadline:
        sleep(0.02)
    stop_recording("?뱁솕瑜?以묒??덉뒿?덈떎.")
    if filename:
        target = RECORDINGS_DIR / Path(filename).name
        if target.exists():
            finished["file_size"] = target.stat().st_size
            finished["download_url"] = f"{PUBLIC_BASE_URL}/api/recordings/{target.name}"
            finished["download_delete_url"] = f"{PUBLIC_BASE_URL}/api/recordings/{target.name}?delete_after=1"
            print(
                f"[recording] stopped file={target} frames={finished.get('frames_written', 0)} "
                f"bytes={finished.get('file_size', 0)}"
            )
        else:
            print(f"[recording] stopped but file is missing: {target}")

    deleted = cleanup_recordings()
    return {
        "ok": True,
        "recording": finished,
        "download_url": f"{PUBLIC_BASE_URL}/api/recordings/{filename}" if filename else "",
        "download_delete_url": f"{PUBLIC_BASE_URL}/api/recordings/{filename}?delete_after=1" if filename else "",
        "storage_bytes": recording_storage_bytes(),
        "cleanup_deleted": deleted,
        "recent_files": list_recordings(),
    }


@app.get("/api/recordings")
def recordings() -> dict[str, Any]:
    deleted = cleanup_recordings()
    return {
        "ok": True,
        "items": list_recordings(),
        "storage_bytes": recording_storage_bytes(),
        "max_storage_bytes": MAX_RECORDING_BYTES,
        "cleanup_deleted": deleted,
    }


@app.get("/api/recordings/{filename}")
def recording_download(filename: str, delete_after: bool = False):
    safe_name = Path(filename).name
    target = RECORDINGS_DIR / safe_name
    if not target.exists() or not target.is_file():
        return {"ok": False, "message": "file not found"}
    response = FileResponse(
        path=target,
        media_type="multipart/x-mixed-replace; boundary=frame",
        filename=safe_name,
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )
    if delete_after:
        def remove_after_send() -> None:
            with suppress(FileNotFoundError):
                target.unlink()

        response.background = BackgroundTask(remove_after_send)
    return response


@app.delete("/api/recordings/{filename}")
def recording_delete(filename: str) -> dict[str, Any]:
    safe_name = Path(filename).name
    target = RECORDINGS_DIR / safe_name
    if not target.exists() or not target.is_file():
        return {"ok": False, "message": "file not found"}
    target.unlink()
    return {
        "ok": True,
        "deleted": safe_name,
        "storage_bytes": recording_storage_bytes(),
        "recent_files": list_recordings(),
    }


if __name__ == "__main__":
    try:
        import uvicorn
    except ModuleNotFoundError:
        print("[error] uvicorn is not installed.")
        print("[hint] Install dependencies first: pip install fastapi uvicorn")
        raise SystemExit(1)

    port = int(os.getenv("PORT", "8012"))
    uvicorn.run("camera_server:app", host="0.0.0.0", port=port, reload=False)