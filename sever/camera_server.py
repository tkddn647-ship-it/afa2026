import asyncio
import json
import struct
import base64
import binascii
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
import zipfile
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from fastapi import FastAPI, Request
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

app = FastAPI()
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
RECORDINGS_DIR = BASE_DIR / "camera_recordings"
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

STREAM_IDLE_SLEEP_SEC = float(os.getenv("CAMERA_STREAM_IDLE_SLEEP_SEC", "0.01"))
STREAM_HEARTBEAT_SEC = float(os.getenv("CAMERA_STREAM_HEARTBEAT_SEC", "1.0"))
DEFAULT_RECORD_FPS = float(os.getenv("CAMERA_RECORD_FPS", "24"))
DEFAULT_RECORD_FORMAT = os.getenv("CAMERA_RECORD_FORMAT", "mp4").strip().lower()
ZIP_RECORDINGS = os.getenv("CAMERA_RECORD_ZIP", "1").strip().lower() in {"1", "true", "yes", "on"}
RECORD_EXTENSIONS = (".zip", ".mp4", ".mjpg", ".avi")
PUBLIC_BASE_URL = os.getenv("CAMERA_PUBLIC_BASE_URL", "http://3.39.188.80:8012").rstrip("/")
MAX_RECORDING_BYTES = int(os.getenv("CAMERA_MAX_RECORDING_BYTES", str(1024 * 1024 * 1024)))
FFMPEG_FINALIZE_TIMEOUT_SEC = int(os.getenv("CAMERA_RECORD_FFMPEG_TIMEOUT_SEC", "600"))
LOCAL_FFMPEG = BASE_DIR / "bin" / "ffmpeg"
LOGGER_SNAPSHOT_URL = os.getenv("LOGGER_SNAPSHOT_URL", "http://127.0.0.1:8011/api/live/snapshot").strip()
TELEMETRY_POLL_MS = int(os.getenv("CAMERA_TELEMETRY_POLL_MS", "100"))

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


class AviMjpegWriter:
    """Motion JPEG in a standard AVI container (VLC, Windows Media Player compatible)."""

    def __init__(self, path: Path, width: int, height: int, fps: float) -> None:
        self.path = path
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.fps = max(1.0, float(fps))
        self.frame_count = 0
        self._index: list[tuple[bytes, int, int]] = []
        self.fp = path.open("wb")
        self._riff_size_pos = 0
        self._hdrl_list_size_pos = 0
        self._movi_list_size_pos = 0
        self._strl_list_size_pos = 0
        self._avih_total_frames_pos = 0
        self._strh_length_pos = 0
        self._write_header_shell()

    @staticmethod
    def _u32(value: int) -> bytes:
        return struct.pack("<I", value & 0xFFFFFFFF)

    def _write_chunk(self, fourcc: bytes, data: bytes) -> None:
        pad = b"\x00" if len(data) % 2 else b""
        self.fp.write(fourcc)
        self.fp.write(self._u32(len(data)))
        self.fp.write(data)
        if pad:
            self.fp.write(pad)

    def _write_list_open(self, list_type: bytes) -> int:
        self.fp.write(b"LIST")
        size_pos = self.fp.tell()
        self.fp.write(self._u32(0))
        self.fp.write(list_type)
        return size_pos

    def _patch_u32(self, pos: int, value: int) -> None:
        self.fp.seek(pos)
        self.fp.write(self._u32(value))
        self.fp.seek(0, 2)

    def _write_header_shell(self) -> None:
        self.fp.write(b"RIFF")
        self._riff_size_pos = self.fp.tell()
        self.fp.write(self._u32(0))
        self.fp.write(b"AVI ")

        self._hdrl_list_size_pos = self._write_list_open(b"hdrl")

        us_per_frame = max(1, int(1_000_000 / self.fps))
        avih = struct.pack(
            "<13I",
            us_per_frame,
            max(1, self.width * self.height * 4),
            0,
            0x10,
            0,
            0,
            1,
            0,
            self.width,
            self.height,
            0,
            0,
            max(1, self.width * self.height * 3),
        )
        self.fp.write(b"avih")
        self.fp.write(self._u32(len(avih)))
        self._avih_total_frames_pos = self.fp.tell() + 16
        self.fp.write(avih)

        self._strl_list_size_pos = self._write_list_open(b"strl")

        strh = bytearray(56)
        strh[0:4] = b"vids"
        strh[4:8] = b"MJPG"
        struct.pack_into("<I", strh, 20, 1)
        struct.pack_into("<I", strh, 24, max(1, int(round(self.fps))))
        struct.pack_into("<I", strh, 32, 0)
        struct.pack_into("<I", strh, 36, max(1, self.width * self.height * 3))
        struct.pack_into("<i", strh, 40, -1)
        struct.pack_into("<hhhh", strh, 48, 0, 0, self.width, self.height)
        strh_data_pos = self.fp.tell() + 8
        self._write_chunk(b"strh", bytes(strh))
        self._strh_length_pos = strh_data_pos + 32

        strf = struct.pack(
            "<IiiHH4sIiiII",
            40,
            self.width,
            self.height,
            1,
            24,
            b"MJPG",
            0,
            0,
            0,
            0,
            0,
        )
        self._write_chunk(b"strf", strf)

        strl_end = self.fp.tell()
        self._patch_u32(self._strl_list_size_pos, strl_end - self._strl_list_size_pos - 4)

        hdrl_end = self.fp.tell()
        self._patch_u32(self._hdrl_list_size_pos, hdrl_end - self._hdrl_list_size_pos - 4)

        self._movi_list_size_pos = self._write_list_open(b"movi")

    def write(self, jpeg_bytes: bytes) -> None:
        if not jpeg_bytes:
            raise RuntimeError("empty jpeg frame")
        offset = self.fp.tell()
        self._write_chunk(b"00db", jpeg_bytes)
        self._index.append((b"00db", offset, len(jpeg_bytes)))
        self.frame_count += 1

    def release(self) -> None:
        if self.fp.closed:
            return

        movi_end = self.fp.tell()
        self._patch_u32(self._movi_list_size_pos, movi_end - self._movi_list_size_pos - 4)

        idx_data = bytearray()
        for ckid, offset, length in self._index:
            idx_data += ckid
            idx_data += self._u32(0x10)
            idx_data += self._u32(offset)
            idx_data += self._u32(length)
        self._write_chunk(b"idx1", bytes(idx_data))

        self._patch_u32(self._avih_total_frames_pos, self.frame_count)
        self._patch_u32(self._strh_length_pos, self.frame_count)

        file_size = self.fp.tell()
        self._patch_u32(self._riff_size_pos, file_size - 8)
        self.fp.flush()
        self.fp.close()

        if self.frame_count <= 0 or not self.path.exists() or self.path.stat().st_size <= 0:
            raise RuntimeError("avi file was not created")


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


class FfmpegMp4Writer:
    def __init__(self, path: Path, fps: float) -> None:
        self.path = path
        ffmpeg_bin = _ffmpeg_binary()
        if not ffmpeg_bin:
            raise RuntimeError("ffmpeg not found")
        self.proc = subprocess.Popen(
            [
                ffmpeg_bin,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "-framerate",
                f"{max(1.0, fps):g}",
                "-i",
                "pipe:0",
                "-an",
                "-c:v",
                "libx264",
                "-crf",
                os.getenv("CAMERA_RECORD_CRF", "28"),
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def write(self, jpeg_bytes: bytes) -> None:
        if self.proc.stdin is None:
            raise RuntimeError("ffmpeg stdin is not available")
        self.proc.stdin.write(jpeg_bytes)
        self.proc.stdin.flush()
        if self.proc.poll() is not None:
            stderr = ""
            if self.proc.stderr is not None:
                stderr = self.proc.stderr.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(stderr or "ffmpeg process exited early")

    def release(self) -> None:
        stderr_text = ""
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()
        if self.proc.stderr is not None:
            stderr_text = self.proc.stderr.read().decode("utf-8", errors="replace").strip()
        try:
            return_code = self.proc.wait(timeout=max(30, FFMPEG_FINALIZE_TIMEOUT_SEC))
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise RuntimeError("ffmpeg mp4 encoding timed out")

        if return_code != 0:
            raise RuntimeError(stderr_text or f"ffmpeg exited with code {return_code}")

        if not self.path.exists() or self.path.stat().st_size <= 0:
            raise RuntimeError("mp4 file was not created")


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


def _jpeg_dimensions(jpeg_bytes: bytes) -> tuple[int, int]:
    index = 2
    size = len(jpeg_bytes)
    while index < size - 8:
        if jpeg_bytes[index] != 0xFF:
            index += 1
            continue
        marker = jpeg_bytes[index + 1]
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            height = (jpeg_bytes[index + 5] << 8) + jpeg_bytes[index + 6]
            width = (jpeg_bytes[index + 7] << 8) + jpeg_bytes[index + 8]
            return width, height
        if marker in {0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7}:
            index += 4
            continue
        if marker == 0xD9:
            break
        segment_len = (jpeg_bytes[index + 2] << 8) + jpeg_bytes[index + 3]
        index += 2 + segment_len
    return 0, 0


def _iter_recording_files() -> list[Path]:
    files: list[Path] = []
    for ext in RECORD_EXTENSIONS:
        files.extend(path for path in RECORDINGS_DIR.glob(f"*{ext}") if path.is_file())
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)


def _recording_media_type(filename: str) -> str:
    lowered = filename.lower()
    if lowered.endswith(".zip"):
        return "application/zip"
    if lowered.endswith(".mp4"):
        return "video/mp4"
    if lowered.endswith(".avi"):
        return "video/x-msvideo"
    return "multipart/x-mixed-replace; boundary=frame"


def _is_playable_recording(filename: str) -> bool:
    lowered = filename.lower()
    return lowered.endswith(".mp4") or lowered.endswith(".avi") or lowered.endswith(".mjpg")


def _zip_recording_file(source: Path) -> tuple[Path, dict[str, Any]]:
    original_bytes = source.stat().st_size
    meta: dict[str, Any] = {
        "compressed": False,
        "archive": "",
        "original_bytes": original_bytes,
        "stored_bytes": original_bytes,
    }
    if not ZIP_RECORDINGS or source.suffix.lower() == ".zip":
        return source, meta

    zip_path = _unique_recording_path(f"{source.stem}.zip", ".zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.write(source, arcname=source.name)

    zip_bytes = zip_path.stat().st_size
    # MP4는 이미 압축돼 있어 ZIP 효과가 거의 없음. 줄어들 때만 ZIP 유지.
    if zip_bytes < original_bytes:
        source.unlink()
        meta.update(
            {
                "compressed": True,
                "archive": zip_path.name,
                "stored_bytes": zip_bytes,
                "saved_bytes": original_bytes - zip_bytes,
            }
        )
        print(
            f"[recording] zipped {source.name} -> {zip_path.name} "
            f"({original_bytes} -> {zip_bytes} bytes)"
        )
        return zip_path, meta

    zip_path.unlink()
    print(
        f"[recording] kept {source.name} (zip did not reduce size: "
        f"{original_bytes} -> {zip_bytes} bytes)"
    )
    return source, meta


def _finalize_recording_file(source: Path) -> tuple[Path, dict[str, Any]]:
    if not source.exists():
        return source, {"compressed": False, "stored_bytes": 0}
    return _zip_recording_file(source)


def _ffmpeg_binary() -> str:
    custom = os.getenv("CAMERA_FFMPEG_PATH", "").strip()
    if custom and Path(custom).is_file():
        return custom
    if LOCAL_FFMPEG.is_file():
        return str(LOCAL_FFMPEG)
    found = shutil.which("ffmpeg")
    return found or ""


def _ffmpeg_available() -> bool:
    return bool(_ffmpeg_binary())


def _normalize_record_format(fmt: str) -> str:
    lowered = str(fmt or "").strip().lower()
    if lowered == "mjpg":
        return "avi"
    if lowered in {"mp4", "avi"}:
        return lowered
    return "mp4"


def _effective_record_format() -> str:
    return _normalize_record_format(DEFAULT_RECORD_FORMAT)


def recording_storage_bytes() -> int:
    return sum(path.stat().st_size for path in _iter_recording_files())


def cleanup_recordings(max_bytes: int = MAX_RECORDING_BYTES) -> list[str]:
    deleted: list[str] = []
    files = sorted(_iter_recording_files(), key=lambda path: path.stat().st_mtime)
    total = sum(path.stat().st_size for path in files)
    for path in files:
        if total <= max_bytes:
            break
        size = path.stat().st_size
        path.unlink()
        total -= size
        deleted.append(path.name)
    return deleted


def list_recordings(limit: int = 20) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in _iter_recording_files()[:limit]:
        stat = path.stat()
        relative_url = f"/api/recordings/{path.name}"
        playable = _is_playable_recording(path.name)
        play_url = f"{PUBLIC_BASE_URL}{relative_url}?inline=1" if playable else ""
        items.append(
            {
                "name": path.name,
                "format": path.suffix.lstrip(".").lower(),
                "size": stat.st_size,
                "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "url": relative_url,
                "playable": playable,
                "play_url": play_url,
                "download_url": f"{PUBLIC_BASE_URL}{relative_url}",
                "download_delete_url": f"{PUBLIC_BASE_URL}{relative_url}?delete_after=1",
            }
        )
    return items


def latest_camera_bytes() -> tuple[bytes, dict[str, Any]]:
    with camera_lock:
        return camera_frame_bytes, dict(camera_state)


def _unique_recording_path(filename: str, ext: str) -> Path:
    safe_ext = ext if ext.startswith(".") else f".{ext}"
    safe_name = Path(filename).name
    if not safe_name.lower().endswith(safe_ext):
        safe_name = f"{Path(safe_name).stem}{safe_ext}"

    target = RECORDINGS_DIR / safe_name
    counter = 1
    while target.exists():
        target = RECORDINGS_DIR / f"{Path(safe_name).stem}-{counter}{safe_ext}"
        counter += 1
    return target


def _record_frame(frame_bytes: bytes, width: int, height: int) -> bool:
    global recording_writer
    with recording_lock:
        if not recording_state["active"] or recording_writer is None:
            return False
        try:
            recording_writer.write(frame_bytes)
        except Exception as exc:
            print(f"[recording] frame write failed: {exc}")
            recording_state["message"] = f"프레임 저장 실패: {exc}"
            return False
        recording_state["frames_written"] = int(recording_state["frames_written"]) + 1
        recording_state["bytes_written"] = int(recording_state["bytes_written"]) + len(frame_bytes)
        recording_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        return True


def put_recording_frame(frame_bytes: bytes, width: int, height: int) -> None:
    if not recording_state["active"]:
        return
    _record_frame(frame_bytes, width, height)


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
        try:
            writer.release()
        except Exception as exc:
            print(f"[recording] finalize failed: {exc}")
            recording_state["message"] = f"녹화 저장 실패: {exc}"


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
def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="camera.html",
        context={
            "telemetry_poll_ms": max(50, TELEMETRY_POLL_MS),
        },
    )


@app.get("/api/telemetry/snapshot")
def telemetry_snapshot() -> dict[str, Any]:
    try:
        req = urllib.request.Request(LOGGER_SNAPSHOT_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if isinstance(payload, dict):
            return payload
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        print(f"[telemetry] snapshot fetch failed: {exc} url={LOGGER_SNAPSHOT_URL}")
    return {
        "ok": False,
        "speed": 0,
        "accel_x": 0,
        "accel_y": 0,
        "accel_z": 0,
        "core_temp": 0,
        "device": "-",
        "updated_at": "",
        "system": {},
    }


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
            "ffmpeg_available": _ffmpeg_available(),
            "ffmpeg_path": _ffmpeg_binary(),
            "record_format": _effective_record_format(),
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
    if width <= 0 or height <= 0:
        width, height = _jpeg_dimensions(frame_bytes)

    fps = max(1.0, min(60.0, _safe_float(body.get("fps"), DEFAULT_RECORD_FPS)))
    started_at = datetime.now(timezone.utc)
    session_id = started_at.strftime("%Y%m%dT%H%M%SZ")

    if width <= 0 or height <= 0:
        return {"ok": False, "message": "프레임 크기를 아직 알 수 없어 녹화를 시작할 수 없습니다."}

    record_format = _normalize_record_format(str(body.get("format") or _effective_record_format()))
    if record_format not in {"mp4", "avi"}:
        record_format = _effective_record_format()

    if record_format == "mp4" and not _ffmpeg_available():
        return {
            "ok": False,
            "message": "ffmpeg가 없습니다. EC2에서 sever/install_ffmpeg.sh 실행 후 camera_server를 재시작하세요.",
            "ffmpeg_available": False,
        }

    file_ext = ".mp4" if record_format == "mp4" else ".avi"
    target = _unique_recording_path(str(body.get("filename") or f"camera-{session_id}{file_ext}"), file_ext)
    try:
        if record_format == "mp4":
            writer = FfmpegMp4Writer(target, fps)
        else:
            writer = AviMjpegWriter(target, width, height, fps)
    except Exception as exc:
        if target.exists():
            with suppress(OSError):
                target.unlink()
        print(f"[recording] writer init failed: {exc}")
        return {"ok": False, "message": f"녹화 파일 생성 실패: {exc}"}

    with recording_lock:
        recording_writer = writer
        recording_state.update(
            {
                "active": True,
                "session_id": session_id,
                "filename": target.name,
                "mode": record_format,
                "mime": "video/mp4" if record_format == "mp4" else "video/x-msvideo",
                "frames_written": 0,
                "bytes_written": 0,
                "fps": fps,
                "width": width,
                "height": height,
                "started_at": started_at.isoformat(),
                "updated_at": started_at.isoformat(),
                "message": "녹화를 시작했습니다.",
            }
        )

    if not _record_frame(frame_bytes, width, height):
        stop_recording("첫 프레임 저장에 실패했습니다.")
        if target.exists():
            with suppress(OSError):
                target.unlink()
        return {"ok": False, "message": recording_state.get("message", "첫 프레임 저장 실패")}

    print(f"[recording] started file={target} size={width}x{height} fps={fps:g} format={record_format}")
    return {"ok": True, **recording_state, "download_url": f"{PUBLIC_BASE_URL}/api/recordings/{target.name}"}


@app.post("/api/recording/stop")
def recording_stop() -> dict[str, Any]:
    with recording_lock:
        if not recording_state["active"]:
            return {"ok": False, "message": "recording is not active", "recent_files": list_recordings()}
        filename = str(recording_state["filename"] or "")
        finished = dict(recording_state)

    deadline = monotonic() + 0.5
    while monotonic() < deadline:
        sleep(0.02)
    stop_recording("녹화를 중지했습니다.")

    frames_written = int(finished.get("frames_written", 0) or 0)
    if frames_written <= 0:
        target = RECORDINGS_DIR / Path(filename).name
        if target.exists():
            with suppress(OSError):
                target.unlink()
        return {
            "ok": False,
            "message": "저장된 프레임이 없습니다. Pi camera.py 실행 여부와 카메라 스트림을 확인하세요.",
            "recording": finished,
            "recent_files": list_recordings(),
        }

    if filename:
        target = RECORDINGS_DIR / Path(filename).name
        if target.exists():
            target, archive_meta = _finalize_recording_file(target)
            filename = target.name
            finished["filename"] = filename
            finished["file_size"] = target.stat().st_size
            finished["archive"] = archive_meta
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
def recording_download(filename: str, delete_after: bool = False, inline: bool = False):
    safe_name = Path(filename).name
    target = RECORDINGS_DIR / safe_name
    if not target.exists() or not target.is_file():
        return {"ok": False, "message": "file not found"}

    disposition = "inline" if inline else "attachment"
    response = FileResponse(
        path=target,
        media_type=_recording_media_type(safe_name),
        filename=safe_name,
        headers={"Content-Disposition": f'{disposition}; filename="{safe_name}"'},
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
        print("[hint] Install dependencies first: pip install fastapi uvicorn jinja2")
        raise SystemExit(1)

    effective_format = _effective_record_format()
    ffmpeg_bin = _ffmpeg_binary()
    if effective_format == "mp4":
        if ffmpeg_bin:
            print(f"[recording] format=mp4 ffmpeg={ffmpeg_bin}")
        else:
            print("[recording] format=mp4 but ffmpeg not found")
            print("[hint] Run: bash install_ffmpeg.sh")
    print(f"[telemetry] logger={LOGGER_SNAPSHOT_URL} poll={TELEMETRY_POLL_MS}ms")

    port = int(os.getenv("PORT", "8012"))
    uvicorn.run("camera_server:app", host="0.0.0.0", port=port, reload=False)