import asyncio
import csv
import gzip
import json
import os
import random
import re
import secrets
import shutil
import urllib.error
import urllib.request
import zipfile
from collections import deque
from contextlib import suppress
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from time import monotonic, time
from typing import Any


def _load_env_file(filename: str = "env.ingest") -> None:
    env_path = Path(__file__).resolve().parent / filename
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file()

import sys

_ROOT_DIR = Path(__file__).resolve().parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.templating import Jinja2Templates

from offset_shared import (
    apply_offsets_to_parsed,
    extract_offset_candidates,
    raw_value_to_offset,
    snapshot_to_offsets,
)
from afa_auth import install_auth
from nav_config import nav_context

SAMPLE_TIMES_MAXLEN = int(os.getenv("SAMPLE_TIMES_MAXLEN", "2000"))
LIVE_TASK_MAX = int(os.getenv("LIVE_TASK_MAX", "64"))
DEVICE_CACHE_MAX = int(os.getenv("DEVICE_CACHE_MAX", "32"))
COMPLETED_DOWNLOADS_MAX = int(os.getenv("COMPLETED_DOWNLOADS_MAX", "8"))
LOG_ENQUEUE_BATCH_SIZE = int(os.getenv("LOG_ENQUEUE_BATCH_SIZE", "32"))
AFA_WS_MAX_SIZE = int(os.getenv("AFA_WS_MAX_SIZE", str(1 << 20)))

app = FastAPI()
install_auth(app)
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = Path(os.getenv("LOG_DIR", str(BASE_DIR / "logs")))
LOG_DIR.mkdir(parents=True, exist_ok=True)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
ws_clients: set[WebSocket] = set()
sample_times: deque[float] = deque(maxlen=max(100, SAMPLE_TIMES_MAXLEN))
sample_times_by_platform: dict[str, deque[float]] = {
    "esp32": deque(maxlen=max(100, SAMPLE_TIMES_MAXLEN)),
    "raspberry_pi": deque(maxlen=max(100, SAMPLE_TIMES_MAXLEN)),
}
total_packets = 0
total_samples = 0
logging_enabled = False
last_sample_ms_by_device: dict[str, int] = {}
sample_index_by_device: dict[str, int] = {}
logging_started_epoch_ms = 0
device_last_seen_epoch_ms: dict[str, int] = {}
device_offsets: dict[str, dict[str, float]] = {}
wheel_config: dict[str, int | float] = {
    "teeth_count": 0,
    "sample_period_ms": 5.0,
}
wheel_last_sample_ms_by_device: dict[str, float] = {}
live_tasks: set[asyncio.Task[Any]] = set()
live_tasks_dropped = 0
DEVICE_ONLINE_WINDOW_MS = 10000
MONITOR_FORWARD_URL = os.getenv("MONITOR_FORWARD_URL", os.getenv("FORWARD_URL", "")).strip()
FORWARD_TIMEOUT_SEC = float(os.getenv("FORWARD_TIMEOUT_SEC", "2.0"))
FORWARD_QUEUE_MAX = int(os.getenv("FORWARD_QUEUE_MAX", "200"))
FORWARD_HEADER = "X-UDP-Logger-Forwarded"
AFA_SOCKET_URL = os.getenv(
    "AFA_SOCKET_URL",
    "",
).strip()
AFA_SOCKET_EVENT = os.getenv("AFA_SOCKET_EVENT", "sensor_update").strip() or "sensor_update"
SIM_LINEAR_ENABLED = os.getenv("SIM_LINEAR_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
SIM_LINEAR_INTERVAL_MS = int(os.getenv("SIM_LINEAR_INTERVAL_MS", "100"))
ENABLE_UDP = os.getenv("ENABLE_UDP", "1").strip().lower() in {"1", "true", "yes", "on"}
ENABLE_TCP = os.getenv("ENABLE_TCP", "1").strip().lower() in {"1", "true", "yes", "on"}
UDP_BIND_HOST = os.getenv("UDP_BIND_HOST", "0.0.0.0").strip() or "0.0.0.0"
UDP_BIND_PORT = int(os.getenv("UDP_BIND_PORT", "9999"))
TCP_BIND_HOST = os.getenv("TCP_BIND_HOST", "0.0.0.0").strip() or "0.0.0.0"
TCP_BIND_PORT = int(os.getenv("TCP_BIND_PORT", str(UDP_BIND_PORT)))
LOG_QUEUE_MAX_ROWS = int(os.getenv("LOG_QUEUE_MAX_ROWS", "1000"))
LOG_FLUSH_INTERVAL_SEC = float(os.getenv("LOG_FLUSH_INTERVAL_SEC", "1.0"))
LOG_FLUSH_ROW_BATCH = int(os.getenv("LOG_FLUSH_ROW_BATCH", "100"))
LOG_ROLL_SECONDS = int(os.getenv("LOG_ROLL_SECONDS", "0"))
LOG_ROLL_SIZE_BYTES = int(os.getenv("LOG_ROLL_SIZE_BYTES", "0"))
LOG_FSYNC_INTERVAL_SEC = float(os.getenv("LOG_FSYNC_INTERVAL_SEC", "0"))
LOG_DOWNLOAD_GZIP = os.getenv("LOG_DOWNLOAD_GZIP", "1").strip().lower() in {"1", "true", "yes", "on"}
LOG_GZIP_LEVEL = int(os.getenv("LOG_GZIP_LEVEL", "6"))
LOG_DELETE_AFTER_DOWNLOAD = os.getenv("LOG_DELETE_AFTER_DOWNLOAD", "1").strip().lower() in {"1", "true", "yes", "on"}
LOG_MAX_DIR_BYTES = int(os.getenv("LOG_MAX_DIR_BYTES", str(512 * 1024 * 1024)))
LOG_MIN_FREE_BYTES = int(os.getenv("LOG_MIN_FREE_BYTES", str(64 * 1024 * 1024)))
CAMERA_INTERNAL_URL = os.getenv("CAMERA_INTERNAL_URL", "http://127.0.0.1:8012").strip().rstrip("/")
LINK_CAMERA_RECORDING = os.getenv("LINK_CAMERA_RECORDING", "1").strip().lower() in {"1", "true", "yes", "on"}
CAMERA_API_TIMEOUT_SEC = float(os.getenv("CAMERA_API_TIMEOUT_SEC", "3.0"))
forward_success_count = 0
forward_failure_count = 0
forward_last_error = ""
forward_last_sent_at = ""
forward_dropped_count = 0
afa_forward_success_count = 0
afa_forward_failure_count = 0
afa_forward_last_error = ""
afa_forward_last_sent_at = ""
sim_linear_packets = 0
latest_sensor_snapshot_by_device: dict[str, dict[str, float]] = {}
forward_queue: "asyncio.Queue[dict[str, Any]] | None" = None
LOG_FIELDNAMES = [
    "timestamp",
    "stm_time_ns",
    "speed",
    "wheel_rpm",
    "wheel_pulse",
    "wheel_teeth",
    "accelX",
    "accelY",
    "accelZ",
    "front_Tire",
    "rear_Tire",
    "accel_p",
    "break_p",
    "steering_speed",
    "steering_angle",
    "linear_fl",
    "linear_fr",
    "linear_rl",
    "linear_rr",
    "ECU_temp",
    "INV_TEMP_IGT",
    "INV_TEMP_RTD1",
    "INV_TEMP_RTD2",
    "INV_TEMP_gatedriver",
    "INV_TEMP_controlboard",
    "INV_TEMP_coolant",
    "INV_TEMP_hotspot",
    "INV_TEMP_motor",
    "INV_moter_speed",
    "INV_moter_angle",
    "INV_dc_currnet",
    "INV_A_currnet",
    "INV_B_currnet",
    "INV_C_currnet",
    "INV_voltage",
    "INV_voltage_output",
    "INV_torque_feedback",
    "INV_torque_commanded",
    "INV_id_feedback",
    "INV_iq_feedback",
    "BMS_charge",
    "BMS_capacity",
    "BMS_voltage",
    "BMS_current",
    "BMS_ccl",
    "BMS_dcl",
    "BMS_TEMP_maxvalue",
    "BMS_TEMP_maxid",
    "BMS_TEMP_minvalue",
    "BMS_TEMP_minid",
    "BMS_TEMP_internal",
    "tp",
    "hm",
]
logging_session: "CsvLogSession | None" = None
completed_downloads: dict[str, dict[str, Any]] = {}

LINEAR_SENSOR_PATHS = {
    "front_left": "car.linear.front_left",
    "front_right": "car.linear.front_right",
    "rear_left": "car.linear.rear_left",
    "rear_right": "car.linear.rear_right",
    "fl": "car.linear.front_left",
    "fr": "car.linear.front_right",
    "rl": "car.linear.rear_left",
    "rr": "car.linear.rear_right",
}

ACCEL_SENSOR_PATHS = {
    "x": "car.accel.x",
    "y": "car.accel.y",
    "z": "car.accel.z",
    "ax": "car.accel.x",
    "ay": "car.accel.y",
    "az": "car.accel.z",
}

WHEEL_PULSE_ALIASES = (
    "pulse",
    "pulses",
    "pulse_count",
    "wheel_pulse",
    "wheel_pulses",
    "wheel_pulse_count",
    "tone_pulse",
    "tone_pulses",
    "tone_pulse_count",
)

WHEEL_PULSE_PATHS = {
    "car.wheel.pulse",
    "car.wheel.pulses",
    "car.wheel.pulse_count",
    "car.wheel_speed.pulse",
    "car.wheel_speed.pulses",
}


def _disk_usage(path: Path | str) -> dict[str, int]:
    usage = shutil.disk_usage(Path(path).resolve())
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
    }


def _disk_free_ok(path: Path | str, min_free: int = LOG_MIN_FREE_BYTES) -> tuple[bool, dict[str, int]]:
    stats = _disk_usage(path)
    return stats["free_bytes"] >= min_free, stats


def cleanup_log_dir(
    max_bytes: int = LOG_MAX_DIR_BYTES,
    *,
    exclude: set[str] | None = None,
) -> list[str]:
    exclude_resolved = exclude or set()
    files = sorted(
        [path for path in LOG_DIR.glob("sensor_log_*") if path.is_file()],
        key=lambda path: path.stat().st_mtime,
    )
    deleted: list[str] = []
    remaining: list[Path] = []
    for path in files:
        if path.stat().st_size == 0 and str(path.resolve()) not in exclude_resolved:
            path.unlink(missing_ok=True)
            deleted.append(path.name)
            continue
        remaining.append(path)
    files = remaining
    total = sum(path.stat().st_size for path in files)
    while total > max_bytes and files:
        victim = files.pop(0)
        if str(victim.resolve()) in exclude_resolved:
            continue
        size = victim.stat().st_size
        victim.unlink(missing_ok=True)
        total -= size
        deleted.append(victim.name)
    return deleted


def _delete_log_file(path_str: str) -> None:
    try:
        Path(path_str).unlink(missing_ok=True)
    except OSError:
        pass


def _gzip_is_valid(path: Path) -> bool:
    if not path.is_file() or not path.name.endswith(".gz"):
        return path.is_file()
    try:
        with gzip.open(path, "rb") as handle:
            while handle.read(1024 * 1024):
                pass
        return True
    except Exception:
        return False


def _gzip_csv_file(csv_path: Path) -> Path:
    gz_path = Path(f"{csv_path}.gz")
    with csv_path.open("rb") as src, gzip.open(
        gz_path,
        "wb",
        compresslevel=max(1, min(9, LOG_GZIP_LEVEL)),
    ) as dst:
        shutil.copyfileobj(src, dst)
    csv_path.unlink(missing_ok=True)
    return gz_path


def _salvage_truncated_gzip(src: Path) -> Path | None:
    if not src.is_file() or not src.name.endswith(".gz"):
        return None
    dst = Path(str(src)[:-3])
    lines_written = 0
    try:
        with gzip.open(src, "rt", encoding="utf-8", newline="") as src_file, dst.open(
            "w", encoding="utf-8", newline=""
        ) as dst_file:
            for line in src_file:
                dst_file.write(line)
                lines_written += 1
    except Exception:
        pass
    if lines_written < 1 or not dst.is_file():
        dst.unlink(missing_ok=True)
        return None
    return dst


def _delete_download_payload(payload: dict[str, Any]) -> None:
    _delete_log_file(str(payload.get("file_path", "")))
    part_files = payload.get("part_files")
    if not isinstance(part_files, list):
        return
    for part in part_files:
        if isinstance(part, dict):
            _delete_log_file(str(part.get("file_path", "")))


def _store_completed_download(download_token: str, download_payload: dict[str, Any]) -> None:
    completed_downloads[download_token] = {
        **download_payload,
        "stopped_at": datetime.now(timezone.utc).isoformat(),
    }
    while len(completed_downloads) > max(1, COMPLETED_DOWNLOADS_MAX):
        old_token, _old_payload = next(iter(completed_downloads.items()))
        completed_downloads.pop(old_token, None)


def _completed_sessions_public() -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for token, payload in completed_downloads.items():
        if not isinstance(payload, dict):
            continue
        sessions.append(
            {
                "token": token,
                "filename": payload.get("filename", ""),
                "download_bytes": payload.get("download_bytes", 0),
                "compressed": bool(payload.get("compressed", LOG_DOWNLOAD_GZIP)),
                "part_count": int(payload.get("part_count", 1) or 1),
                "is_archive": bool(payload.get("is_archive", False)),
                "stopped_at": payload.get("stopped_at", ""),
                "download_url": f"/api/logging/download/{token}",
            }
        )
    sessions.sort(key=lambda item: item.get("stopped_at", ""), reverse=True)
    return sessions


def _registered_log_paths() -> set[str]:
    registered: set[str] = set()
    for payload in completed_downloads.values():
        if not isinstance(payload, dict):
            continue
        file_path = str(payload.get("file_path", "")).strip()
        if file_path:
            registered.add(file_path)
        part_files = payload.get("part_files")
        if isinstance(part_files, list):
            for part in part_files:
                if isinstance(part, dict):
                    part_path = str(part.get("file_path", "")).strip()
                    if part_path:
                        registered.add(part_path)
    return registered


def _active_log_paths() -> set[str]:
    if logging_session is None:
        return set()
    active: set[str] = set()
    for filename in logging_session.all_filenames():
        if filename:
            active.add(str((LOG_DIR / filename).resolve()))
    current = logging_session.file_path
    if current is not None:
        active.add(str(current.resolve()))
    return active


def _build_zip_archive_paths(paths: list[Path], zip_name: str) -> Path:
    zip_path = LOG_DIR / zip_name
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            if path.is_file():
                archive.write(path, arcname=path.name)
    return zip_path


def _part_payload(path: Path) -> dict[str, Any]:
    compressed = path.name.endswith(".gz")
    return {
        "filename": path.name,
        "file_path": str(path),
        "compressed": compressed,
        "download_bytes": path.stat().st_size,
    }


def _prepare_recovered_log_path(path: Path) -> Path | None:
    if path.name.endswith(".gz") and not _gzip_is_valid(path):
        salvaged = _salvage_truncated_gzip(path)
        if salvaged is not None:
            print(f"[logging] salvaged truncated gzip: {path.name} -> {salvaged.name}")
            path.unlink(missing_ok=True)
            return salvaged
        print(f"[logging] skipped corrupt gzip: {path.name}")
        return None
    return path


def _recover_orphan_log_files() -> int:
    if not LOG_DIR.is_dir():
        return 0

    registered_paths = _registered_log_paths()
    active_paths = _active_log_paths()
    groups: dict[str, list[Path]] = {}
    recovered = 0

    for path in sorted(LOG_DIR.iterdir()):
        if not path.is_file() or not path.name.startswith("sensor_log_"):
            continue
        resolved = str(path.resolve())
        if resolved in registered_paths or resolved in active_paths:
            continue

        if path.suffix == ".zip":
            token = secrets.token_urlsafe(16)
            stopped_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
            _store_completed_download(
                token,
                {
                    "filename": path.name,
                    "file_path": resolved,
                    "compressed": False,
                    "download_bytes": path.stat().st_size,
                    "part_count": 1,
                    "part_files": [{"filename": path.name, "file_path": resolved}],
                    "is_archive": True,
                },
            )
            completed_downloads[token]["stopped_at"] = stopped_at
            recovered += 1
            registered_paths.add(resolved)
            continue

        match = re.match(r"sensor_log_(\d{8}_\d{6})", path.name)
        session_key = match.group(1) if match else path.stem
        groups.setdefault(session_key, []).append(path)

    for session_key, paths in groups.items():
        paths = sorted(paths, key=lambda item: item.name)
        if not paths:
            continue
        if any(str(path.resolve()) in registered_paths for path in paths):
            continue

        token = secrets.token_urlsafe(16)
        stopped_at = datetime.fromtimestamp(
            max(path.stat().st_mtime for path in paths),
            tz=timezone.utc,
        ).isoformat()

        registered_now: list[Path] = []
        if len(paths) == 1:
            ready_path = _prepare_recovered_log_path(paths[0])
            if ready_path is None:
                continue
            registered_now = [ready_path]
            part = _part_payload(ready_path)
            payload = {
                **part,
                "part_count": 1,
                "part_files": [part],
                "is_archive": False,
            }
        else:
            ready_paths = []
            seen_paths: set[str] = set()
            for path in paths:
                ready_path = _prepare_recovered_log_path(path)
                if ready_path is None:
                    continue
                resolved = str(ready_path.resolve())
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)
                ready_paths.append(ready_path)
            if not ready_paths:
                continue
            registered_now = ready_paths
            zip_name = f"sensor_log_{session_key}.zip"
            zip_path = LOG_DIR / zip_name
            if not zip_path.is_file():
                _build_zip_archive_paths(ready_paths, zip_name)
            payload = {
                "filename": zip_name,
                "file_path": str(zip_path.resolve()),
                "compressed": False,
                "download_bytes": zip_path.stat().st_size,
                "part_count": len(ready_paths),
                "part_files": [{"filename": zip_name, "file_path": str(zip_path.resolve())}],
                "is_archive": True,
            }
            registered_paths.add(str(zip_path.resolve()))

        _store_completed_download(token, payload)
        completed_downloads[token]["stopped_at"] = stopped_at
        recovered += 1
        for path in registered_now:
            registered_paths.add(str(path.resolve()))

    return recovered


class CsvLogSession:
    def __init__(self, *, started_epoch_ms: int) -> None:
        self.started_epoch_ms = started_epoch_ms
        self.started_at = datetime.now(timezone.utc)
        self.session_id = self.started_at.strftime("%Y%m%d_%H%M%S")
        self.queue: asyncio.Queue[list[dict[str, Any]]] = asyncio.Queue(maxsize=max(1, LOG_QUEUE_MAX_ROWS))
        self.pending_rows: list[dict[str, Any]] = []
        self.stop_requested = False
        self.writer_task: asyncio.Task[None] | None = None
        self.file_handle: Any = None
        self.writer: csv.DictWriter | None = None
        self.rows_written = 0
        self.rows_enqueued = 0
        self.dropped_rows = 0
        self.current_part_rows_written = 0
        self.part_number = 0
        self.part_files: list[dict[str, Any]] = []
        self.current_part_opened_at = 0.0
        self.last_fsync_at = 0.0
        self.last_error = ""
        self.closed = False
        self.download_filename = ""
        self.file_path = LOG_DIR / f"sensor_log_{self.session_id}.csv"

    def _file_extension(self) -> str:
        return ".csv"

    def _build_part_filename(self, part_number: int) -> str:
        suffix = self._file_extension()
        if self._rolling_enabled():
            return f"sensor_log_{self.session_id}_part{part_number:03d}{suffix}"
        return f"sensor_log_{self.session_id}{suffix}"

    def _rolling_enabled(self) -> bool:
        return LOG_ROLL_SECONDS > 0 or LOG_ROLL_SIZE_BYTES > 0

    def _open_output_file(self) -> None:
        self.part_number += 1
        self.download_filename = self._build_part_filename(self.part_number)
        self.file_path = LOG_DIR / self.download_filename
        self.current_part_rows_written = 0
        self.current_part_opened_at = monotonic()
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.file_handle = self.file_path.open("w", encoding="utf-8", newline="")
        self.writer = csv.DictWriter(self.file_handle, fieldnames=LOG_FIELDNAMES)
        self.writer.writeheader()

    def _close_output_file(self) -> None:
        if self.file_handle is not None:
            self.file_handle.flush()
            self.file_handle.close()
            self.file_handle = None
        self.writer = None

    def _prepare_part_for_download(self, csv_path: Path) -> Path:
        if not LOG_DOWNLOAD_GZIP:
            return csv_path
        if not csv_path.is_file():
            return csv_path
        gz_path = _gzip_csv_file(csv_path)
        self.download_filename = gz_path.name
        self.file_path = gz_path
        return gz_path

    def _current_part_payload(self) -> dict[str, Any]:
        compressed = self.file_path.name.endswith(".gz")
        return {
            "filename": self.download_filename,
            "file_path": str(self.file_path),
            "compressed": compressed,
            "download_bytes": self.file_bytes(),
            "rows_written": self.current_part_rows_written,
        }

    def _finalize_current_part(self) -> None:
        if self.current_part_rows_written < 1 or not self.file_path.is_file():
            self._close_output_file()
            return
        csv_path = self.file_path
        self._close_output_file()
        self.file_path = csv_path
        self._prepare_part_for_download(csv_path)
        self.part_files.append(self._current_part_payload())

    def _should_roll_by_time(self) -> bool:
        if LOG_ROLL_SECONDS <= 0 or self.current_part_rows_written < 1:
            return False
        return monotonic() - self.current_part_opened_at >= LOG_ROLL_SECONDS

    def _should_roll_by_size(self) -> bool:
        if LOG_ROLL_SIZE_BYTES <= 0 or self.current_part_rows_written < 1:
            return False
        return self.file_bytes() >= LOG_ROLL_SIZE_BYTES

    def _roll_part(self) -> None:
        if not self._rolling_enabled():
            return
        self._flush_pending(force=True)
        if self.current_part_rows_written < 1:
            self.current_part_opened_at = monotonic()
            return
        self._finalize_current_part()
        self._open_output_file()

    def _maybe_fsync(self) -> None:
        if LOG_FSYNC_INTERVAL_SEC <= 0 or self.file_handle is None:
            return
        now = monotonic()
        if now - self.last_fsync_at < LOG_FSYNC_INTERVAL_SEC:
            return
        fileno = getattr(self.file_handle, "fileno", None)
        if callable(fileno):
            try:
                os.fsync(fileno())
                self.last_fsync_at = now
            except OSError:
                pass

    def _flush_pending(self, *, force: bool = False) -> None:
        if not self.pending_rows or self.writer is None:
            return

        batch_size = max(1, LOG_FLUSH_ROW_BATCH)
        while self.pending_rows:
            if not force and len(self.pending_rows) < batch_size:
                break
            chunk_size = len(self.pending_rows) if force else min(batch_size, len(self.pending_rows))
            chunk = self.pending_rows[:chunk_size]
            del self.pending_rows[:chunk_size]
            self.writer.writerows(chunk)
            self.rows_written += len(chunk)
            self.current_part_rows_written += len(chunk)
            if self.file_handle is not None:
                self.file_handle.flush()
            self._maybe_fsync()
            if self._should_roll_by_size():
                self._roll_part()
                if self.writer is None:
                    break

    def file_bytes(self) -> int:
        if not self.file_path.is_file():
            return 0
        return self.file_path.stat().st_size

    def total_file_bytes(self) -> int:
        total = sum(int(part.get("download_bytes", 0) or 0) for part in self.part_files)
        if self.current_part_rows_written > 0:
            total += self.file_bytes()
        return total

    def all_filenames(self) -> list[str]:
        names = [str(part.get("filename", "")) for part in self.part_files if part.get("filename")]
        if self.current_part_rows_written > 0 and self.download_filename:
            names.append(self.download_filename)
        return names

    def start(self) -> None:
        self.writer_task = asyncio.create_task(self._writer_loop())

    def status(self) -> dict[str, Any]:
        file_count = len(self.part_files)
        if self.current_part_rows_written > 0:
            file_count += 1
        return {
            "session_id": self.session_id,
            "output_dir": str(LOG_DIR),
            "active_file": self.download_filename if self.current_part_rows_written > 0 else "",
            "files": self.all_filenames(),
            "rows_written": self.rows_written,
            "rows_enqueued": self.rows_enqueued,
            "rows_pending": self.queue.qsize() + len(self.pending_rows),
            "dropped_rows": self.dropped_rows,
            "file_count": file_count,
            "part_number": self.part_number,
            "file_bytes": self.total_file_bytes(),
            "compressed": LOG_DOWNLOAD_GZIP,
            "rolling_enabled": self._rolling_enabled(),
            "roll_seconds": LOG_ROLL_SECONDS,
            "roll_size_bytes": LOG_ROLL_SIZE_BYTES,
            "last_error": self.last_error,
            "closed": self.closed,
        }

    def enqueue_rows(self, rows: list[dict[str, Any]]) -> None:
        if self.stop_requested or self.closed or not rows:
            return
        batch_size = max(1, LOG_ENQUEUE_BATCH_SIZE)
        for start in range(0, len(rows), batch_size):
            chunk = rows[start : start + batch_size]
            try:
                self.queue.put_nowait(chunk)
                self.rows_enqueued += len(chunk)
            except asyncio.QueueFull:
                self.dropped_rows += len(chunk)
                self.last_error = "log writer queue is full"
                return

    def request_stop(self) -> None:
        self.stop_requested = True

    async def close(self) -> None:
        self.request_stop()
        if self.writer_task is not None:
            await self.writer_task
            self.writer_task = None

    async def _writer_loop(self) -> None:
        try:
            self._open_output_file()
            while True:
                if self.stop_requested and self.queue.empty() and not self.pending_rows:
                    break

                timeout = max(0.1, LOG_FLUSH_INTERVAL_SEC)
                try:
                    rows = await asyncio.wait_for(self.queue.get(), timeout=timeout)
                    self.pending_rows.extend(rows)
                except asyncio.TimeoutError:
                    pass

                self._flush_pending()
                if self._should_roll_by_time():
                    self._roll_part()
        except Exception as exc:
            self.last_error = str(exc)
            raise
        finally:
            self._flush_pending(force=True)
            if self.current_part_rows_written > 0:
                self._finalize_current_part()
            else:
                self._close_output_file()
            self.closed = True

    def _collect_all_parts(self) -> list[dict[str, Any]]:
        return list(self.part_files)

    def _build_zip_archive(self, parts: list[dict[str, Any]]) -> Path:
        zip_name = f"sensor_log_{self.session_id}.zip"
        zip_path = LOG_DIR / zip_name
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for part in parts:
                part_path = Path(str(part.get("file_path", "")))
                if part_path.is_file():
                    archive.write(part_path, arcname=str(part.get("filename", part_path.name)))
        return zip_path

    def build_download_response(self) -> Response:
        payload = self.build_download_payload()
        file_path = Path(str(payload.get("file_path", "")))
        if self.rows_written < 1 or not file_path.is_file():
            return Response(
                content=json.dumps({"ok": False, "message": "저장된 파일이 없습니다."}, ensure_ascii=False),
                media_type="application/json",
                status_code=200,
            )
        media_type = "application/zip" if payload.get("is_archive") else (
            "application/gzip" if payload.get("compressed") else "text/csv; charset=utf-8"
        )
        return FileResponse(
            path=file_path,
            media_type=media_type,
            filename=str(payload.get("filename", file_path.name)),
            headers={"X-Download-Bytes": str(payload.get("download_bytes", file_path.stat().st_size))},
        )

    def build_download_payload(self) -> dict[str, Any]:
        parts = self._collect_all_parts()
        if not parts:
            return {
                "filename": "",
                "file_path": "",
                "compressed": LOG_DOWNLOAD_GZIP,
                "download_bytes": 0,
                "part_count": 0,
                "part_files": [],
                "is_archive": False,
            }

        if len(parts) == 1:
            part = parts[0]
            return {
                "filename": part["filename"],
                "file_path": part["file_path"],
                "compressed": bool(part.get("compressed", LOG_DOWNLOAD_GZIP)),
                "download_bytes": int(part.get("download_bytes", 0) or 0),
                "part_count": 1,
                "part_files": parts,
                "is_archive": False,
            }

        zip_path = self._build_zip_archive(parts)
        for part in parts:
            _delete_log_file(str(part.get("file_path", "")))
        return {
            "filename": zip_path.name,
            "file_path": str(zip_path),
            "compressed": False,
            "download_bytes": zip_path.stat().st_size,
            "part_count": len(parts),
            "part_files": [{"filename": zip_path.name, "file_path": str(zip_path)}],
            "is_archive": True,
        }


def record_traffic(sample_count: int = 1) -> None:
    global total_packets, total_samples
    now = monotonic()
    total_packets += 1
    safe_count = max(1, sample_count)
    total_samples += safe_count
    for _ in range(safe_count):
        sample_times.append(now)


def hz_last(window_sec: float = 1.0) -> float:
    now = monotonic()
    cutoff = now - window_sec
    return float(sum(1 for ts in sample_times if ts >= cutoff))


def _platform_key_from_name(name: str) -> str:
    normalized = name.strip().lower()
    if "esp" in normalized:
        return "esp32"
    if "rasp" in normalized or "pi" in normalized:
        return "raspberry_pi"
    return "other"


def _platform_key_from_payload(payload: dict[str, Any]) -> str:
    message = payload.get("message", "")
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        device = parsed.get("device") or parsed.get("d")
        if isinstance(device, str) and device.strip():
            return _platform_key_from_name(device)

    return _platform_key_from_name(_device_key_from_payload(payload))


def record_platform_traffic(payload: dict[str, Any], sample_count: int = 1) -> None:
    platform_key = _platform_key_from_payload(payload)
    if platform_key not in sample_times_by_platform:
        return

    now = monotonic()
    safe_count = max(1, sample_count)
    timestamps = sample_times_by_platform[platform_key]
    for _ in range(safe_count):
        timestamps.append(now)
    cutoff = now - 5.0
    while timestamps and timestamps[0] < cutoff:
        timestamps.popleft()


def hz_last_for_platform(platform_key: str, window_sec: float = 1.0) -> float:
    timestamps = sample_times_by_platform.get(platform_key)
    if not timestamps:
        return 0.0
    now = monotonic()
    cutoff = now - window_sec
    return float(sum(1 for ts in timestamps if ts >= cutoff))


def parse_sample_count(message: str) -> int:
    try:
        payload = json.loads(message)
        if isinstance(payload, dict):
            samples = payload.get("samples")
            if isinstance(samples, list):
                return max(1, len(samples))
    except json.JSONDecodeError:
        pass
    return 1


def _device_key_from_payload(payload: dict[str, Any]) -> str:
    message = payload.get("message", "")
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        device = parsed.get("device") or parsed.get("d")
        if device:
            return str(device)

    return f"{payload.get('ip', 'unknown')}:{payload.get('port', 0)}"


def _prune_device_caches() -> None:
    max_keys = max(8, DEVICE_CACHE_MAX)
    stores = (
        device_last_seen_epoch_ms,
        latest_sensor_snapshot_by_device,
        device_offsets,
        last_sample_ms_by_device,
        sample_index_by_device,
        wheel_last_sample_ms_by_device,
    )
    for store in stores:
        overflow = len(store) - max_keys
        if overflow <= 0:
            continue
        for key in list(store.keys())[:overflow]:
            store.pop(key, None)


def _slim_forward_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "time": payload.get("time", ""),
        "ip": payload.get("ip", ""),
        "port": payload.get("port", 0),
        "transport": payload.get("transport", ""),
        "message": payload.get("message", ""),
    }


def mark_device_seen(payload: dict[str, Any]) -> None:
    device_last_seen_epoch_ms[_device_key_from_payload(payload)] = int(time() * 1000)
    if len(device_last_seen_epoch_ms) > DEVICE_CACHE_MAX:
        online_device_count()
        if len(device_last_seen_epoch_ms) > DEVICE_CACHE_MAX:
            _prune_device_caches()


def online_device_count() -> int:
    now_ms = int(time() * 1000)
    stale_keys = [key for key, seen_ms in device_last_seen_epoch_ms.items() if now_ms - seen_ms > DEVICE_ONLINE_WINDOW_MS]
    for key in stale_keys:
        device_last_seen_epoch_ms.pop(key, None)
    return len(device_last_seen_epoch_ms)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _extract_mcu_die_temp(source: dict[str, Any] | None) -> float | None:
    """STM32 MCU 칩 내부 온도(°C). ingest 샘플 ecu_temp 필드."""
    if not isinstance(source, dict):
        return None
    return _as_float(source.get("ecu_temp"))


def _recv_times_ms(payload: dict[str, Any]) -> tuple[int, str]:
    dt = datetime.now(timezone.utc)
    recv_epoch_ms = int(dt.timestamp() * 1000)
    recv_utc_ms = dt.isoformat(timespec="milliseconds")
    return recv_epoch_ms, recv_utc_ms


def _wheel_teeth_count() -> int:
    teeth_count = _as_int(wheel_config.get("teeth_count"))
    return max(0, teeth_count or 0)


def _wheel_sample_period_ms() -> float:
    sample_period_ms = _as_float(wheel_config.get("sample_period_ms"))
    return max(1.0, sample_period_ms or 5.0)


def _format_recording_timestamp(dt: datetime) -> str:
    return (
        f"{dt.year}/"
        f"{dt.month:02d}/"
        f"{dt.day:02d} "
        f"{dt.hour:02d}:"
        f"{dt.minute:02d}:"
        f"{dt.second:02d}."
        f"{int(dt.microsecond / 1000):03d}"
    )


def _payload_time_to_datetime(payload: dict[str, Any]) -> datetime:
    raw = payload.get("time")
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _extract_stm_time_ns(sample: dict[str, Any]) -> int | str:
    """
    STM 샘플 시간(t/ts/timestamp_ms)을 ns로 변환.
    - STM에서 ms 단위로 보낼 때: ms * 1_000_000
    - 값이 없으면 빈 문자열
    """
    for key in ("t", "ts", "timestamp_ms"):
        sample_ms = _as_int(sample.get(key))
        if sample_ms is not None:
            return int(sample_ms) * 1_000_000
    return ""


def _sim_linear_value(base: float, spread: float = 6.0) -> float:
    value = base + random.uniform(-spread, spread)
    return round(max(0.0, min(100.0, value)), 1)


def build_sim_linear_payload() -> dict[str, Any]:
    linear = {
        "fl": _sim_linear_value(42.0),
        "fr": _sim_linear_value(44.0),
        "rl": _sim_linear_value(38.0),
        "rr": _sim_linear_value(40.0),
    }
    return {
        "time": datetime.now(timezone.utc).isoformat(),
        "ip": "simulator",
        "port": 0,
        "transport": "sim",
        "message": json.dumps({"linear": linear}, ensure_ascii=False),
    }


def forward_enabled() -> bool:
    return bool(MONITOR_FORWARD_URL)


def afa_forward_enabled() -> bool:
    return bool(AFA_SOCKET_URL)


def _is_byte_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, int) and 0 <= item <= 255 for item in value)


def _raw_payload_for_afa(payload: dict[str, Any]) -> Any:
    raw_bytes = payload.get("raw_bytes")
    if _is_byte_list(raw_bytes):
        return raw_bytes

    message = payload.get("message", "")
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        parsed = None

    if _is_byte_list(parsed):
        return parsed

    if isinstance(parsed, dict):
        for key in ("raw", "bytes", "data", "payload"):
            value = parsed.get(key)
            if _is_byte_list(value):
                return value
        return parsed

    return message


def _extract_sensor_updates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    message = payload.get("message", "")
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        return []

    if isinstance(parsed, dict):
        path = parsed.get("path")
        if isinstance(path, str) and "value" in parsed:
            return [{"path": path, "value": parsed.get("value")}]

        updates = parsed.get("sensor_updates")
        if isinstance(updates, list):
            normalized_updates: list[dict[str, Any]] = []
            for item in updates:
                if isinstance(item, dict) and isinstance(item.get("path"), str) and "value" in item:
                    normalized_updates.append({"path": item["path"], "value": item.get("value")})
            if normalized_updates:
                return normalized_updates

        linear = parsed.get("linear")
        if isinstance(linear, dict):
            mapping = {
                "front_left": "car.linear.front_left",
                "front_right": "car.linear.front_right",
                "rear_left": "car.linear.rear_left",
                "rear_right": "car.linear.rear_right",
                "fl": "car.linear.front_left",
                "fr": "car.linear.front_right",
                "rl": "car.linear.rear_left",
                "rr": "car.linear.rear_right",
            }
            linear_updates = []
            for key, sensor_path in mapping.items():
                if key in linear:
                    linear_updates.append({"path": sensor_path, "value": linear.get(key)})
            if linear_updates:
                return linear_updates

        samples = parsed.get("samples")
        if isinstance(samples, list):
            sample_updates: list[dict[str, Any]] = []
            for sample in samples:
                if not isinstance(sample, dict):
                    continue
                path = sample.get("path")
                if isinstance(path, str) and "value" in sample:
                    sample_updates.append({"path": path, "value": sample.get("value")})
            if sample_updates:
                return sample_updates

    return []


def update_latest_sensor_snapshot(payload: dict[str, Any]) -> None:
    message = payload.get("message", "")
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        return

    if not isinstance(parsed, dict):
        return

    snapshot = extract_offset_candidates(parsed)
    if not snapshot:
        return

    device_key = str(parsed.get("device") or parsed.get("d") or _device_key_from_payload(payload))
    latest_sensor_snapshot_by_device[device_key] = snapshot
    if len(latest_sensor_snapshot_by_device) > DEVICE_CACHE_MAX:
        _prune_device_caches()


def _extract_wheel_pulse_from_sample(sample: dict[str, Any]) -> float | None:
    for key in WHEEL_PULSE_ALIASES:
        value = _as_float(sample.get(key))
        if value is not None:
            return value

    car = sample.get("car")
    if isinstance(car, dict):
        for key in WHEEL_PULSE_ALIASES:
            value = _as_float(car.get(key))
            if value is not None:
                return value

    path = sample.get("path")
    if isinstance(path, str) and path in WHEEL_PULSE_PATHS:
        value = _as_float(sample.get("value"))
        if value is not None:
            return value

    updates = sample.get("sensor_updates")
    if isinstance(updates, list):
        for item in updates:
            if not isinstance(item, dict):
                continue
            update_path = item.get("path")
            if isinstance(update_path, str) and update_path in WHEEL_PULSE_PATHS:
                value = _as_float(item.get("value"))
                if value is not None:
                    return value

    return None


def _next_wheel_sample_time_ms(device_key: str, sample_ms: int | None) -> tuple[float | None, float | None]:
    last_sample_ms = wheel_last_sample_ms_by_device.get(device_key)
    if sample_ms is not None:
        current_sample_ms = float(sample_ms)
    elif last_sample_ms is not None:
        current_sample_ms = last_sample_ms + _wheel_sample_period_ms()
    else:
        current_sample_ms = None

    if current_sample_ms is not None:
        wheel_last_sample_ms_by_device[device_key] = current_sample_ms

    return last_sample_ms, current_sample_ms


def _apply_wheel_metrics_to_sample(sample: dict[str, Any], device_key: str, sample_ms: int | None) -> None:
    teeth_count = _wheel_teeth_count()
    if teeth_count < 1:
        return

    pulse_count = _extract_wheel_pulse_from_sample(sample)
    if pulse_count is None:
        return

    previous_ms, current_ms = _next_wheel_sample_time_ms(device_key, sample_ms)
    wheel_rpm = 0.0
    if previous_ms is not None and current_ms is not None:
        delta_ms = current_ms - previous_ms
        if delta_ms > 0:
            wheel_rpm = (pulse_count / float(teeth_count)) * (60000.0 / delta_ms)

    sample["wheel_pulse"] = round(pulse_count, 4)
    sample["wheel_teeth"] = teeth_count
    sample["wheel_rpm"] = round(wheel_rpm, 4)


def apply_offsets_to_payload(payload: dict[str, Any]) -> dict[str, Any]:
    message = payload.get("message", "")
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        return payload

    if not isinstance(parsed, dict):
        return payload

    device_key = str(parsed.get("device") or parsed.get("d") or _device_key_from_payload(payload))
    offsets = device_offsets.get(device_key)
    if not offsets:
        return payload

    if not apply_offsets_to_parsed(parsed, offsets):
        return payload

    return {**payload, "message": json.dumps(parsed, ensure_ascii=False)}


def apply_wheel_speed_to_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if _wheel_teeth_count() < 1:
        return payload

    message = payload.get("message", "")
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        return payload

    if not isinstance(parsed, dict):
        return payload

    device_key = str(parsed.get("device") or parsed.get("d") or _device_key_from_payload(payload))
    changed = False

    samples = parsed.get("samples")
    if isinstance(samples, list) and samples:
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            before_rpm = sample.get("wheel_rpm")
            before_pulse = sample.get("wheel_pulse")
            before_teeth = sample.get("wheel_teeth")
            sample_ms = _as_int(sample.get("t"))
            _apply_wheel_metrics_to_sample(sample, device_key, sample_ms)
            if (
                sample.get("wheel_rpm") != before_rpm
                or sample.get("wheel_pulse") != before_pulse
                or sample.get("wheel_teeth") != before_teeth
            ):
                changed = True
    else:
        before_rpm = parsed.get("wheel_rpm")
        before_pulse = parsed.get("wheel_pulse")
        before_teeth = parsed.get("wheel_teeth")
        sample_ms = _as_int(parsed.get("t"))
        _apply_wheel_metrics_to_sample(parsed, device_key, sample_ms)
        if (
            parsed.get("wheel_rpm") != before_rpm
            or parsed.get("wheel_pulse") != before_pulse
            or parsed.get("wheel_teeth") != before_teeth
        ):
            changed = True

    if not changed:
        return payload

    return {**payload, "message": json.dumps(parsed, ensure_ascii=False)}


def _forward_body(payload: dict[str, Any]) -> bytes:
    message = payload.get("message", "")
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        body = parsed
    else:
        body = {
            "device": _device_key_from_payload(payload),
            "samples": [],
            "raw_message": message,
            "source": {
                "transport": payload.get("transport", ""),
                "ip": payload.get("ip", ""),
                "port": payload.get("port", 0),
                "time": payload.get("time", ""),
            },
        }

    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _post_forward_request(payload: dict[str, Any]) -> None:
    global forward_success_count, forward_failure_count, forward_last_error, forward_last_sent_at

    if not forward_enabled():
        return

    request = urllib.request.Request(
        MONITOR_FORWARD_URL,
        data=_forward_body(payload),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            FORWARD_HEADER: "1",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=FORWARD_TIMEOUT_SEC) as response:
            response.read()
        forward_success_count += 1
        forward_last_error = ""
        forward_last_sent_at = datetime.now(timezone.utc).isoformat()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        forward_failure_count += 1
        forward_last_error = str(exc)


def _camera_api_json(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    if not LINK_CAMERA_RECORDING or not CAMERA_INTERNAL_URL:
        return {"ok": False, "linked": False, "message": "camera linking disabled"}
    url = f"{CAMERA_INTERNAL_URL}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=CAMERA_API_TIMEOUT_SEC) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {"ok": True}
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read().decode("utf-8")
            return json.loads(payload) if payload else {"ok": False, "message": str(exc)}
        except (json.JSONDecodeError, OSError, ValueError):
            return {"ok": False, "message": str(exc)}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "message": str(exc)}


def _camera_recording_filename(session_id: str) -> str:
    return f"camera-{session_id}.mp4"


def _session_sync_path(session_id: str) -> Path:
    return LOG_DIR / f"session_sync_{session_id}.json"


def _write_session_sync(session_id: str, payload: dict[str, Any]) -> Path:
    path = _session_sync_path(session_id)
    existing: dict[str, Any] = {}
    if path.is_file():
        with suppress(OSError, json.JSONDecodeError, ValueError):
            existing = json.loads(path.read_text(encoding="utf-8"))
    merged = {**existing, **payload, "session_id": session_id, "updated_at": datetime.now(timezone.utc).isoformat()}
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    # Mirror next to camera recordings for analysis upload convenience.
    with suppress(OSError):
        cam_dir = Path("/home/ubuntu/udp_realtime/camera_recordings")
        cam_dir.mkdir(parents=True, exist_ok=True)
        (cam_dir / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def _start_linked_camera_recording(session_id: str) -> dict[str, Any]:
    body = {
        "session_id": session_id,
        "filename": _camera_recording_filename(session_id),
        "capture": "camera",
        "max_frames": 0,
    }
    result = _camera_api_json("POST", "/api/recording/start", body)
    if result.get("ok") or result.get("already_active") or result.get("armed"):
        return {**result, "linked": True}
    message = str(result.get("message") or "").lower()
    # RTSP may fail briefly before publisher connects; arm/retry via start is fine for http mode.
    if "no camera frame" in message or "404" in message or "연결" in message or "rtsp" in message:
        armed = _camera_api_json("POST", "/api/recording/arm", body)
        return {**armed, "linked": True, "armed": bool(armed.get("armed") or armed.get("ok"))}
    return {**result, "linked": True}


def _stop_linked_camera_recording() -> dict[str, Any]:
    status = _camera_api_json("GET", "/api/recording/status")
    if status.get("active"):
        return {**_camera_api_json("POST", "/api/recording/stop"), "linked": True}
    _camera_api_json("POST", "/api/recording/disarm")
    return {"ok": True, "linked": True, "armed": False}


def _linked_camera_recording_status() -> dict[str, Any]:
    if not LINK_CAMERA_RECORDING:
        return {"linked": False}
    status = _camera_api_json("GET", "/api/recording/status")
    return {**status, "linked": True}


async def maybe_forward_payload(payload: dict[str, Any], *, skip: bool = False) -> None:
    if skip or not forward_enabled():
        return
    enqueue_forward_payload(payload)


def enqueue_forward_payload(payload: dict[str, Any]) -> None:
    global forward_dropped_count, forward_last_error

    if not forward_enabled():
        return

    queue = forward_queue
    if queue is None:
        forward_dropped_count += 1
        forward_last_error = "forward queue is not ready"
        return

    slim = _slim_forward_payload(payload)
    try:
        queue.put_nowait(slim)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
            queue.put_nowait(slim)
            forward_dropped_count += 1
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            forward_dropped_count += 1
            forward_last_error = "forward queue is full"


async def forward_worker() -> None:
    queue = forward_queue
    if queue is None:
        return

    while True:
        payload = await queue.get()
        try:
            await asyncio.to_thread(_post_forward_request, payload)
        except Exception as exc:
            global forward_failure_count, forward_last_error
            forward_failure_count += 1
            forward_last_error = str(exc)


class AFASocketRelay:
    def __init__(self) -> None:
        self.ws: Any = None
        self.lock = asyncio.Lock()
        self.reader_task: asyncio.Task[None] | None = None

    async def _close_locked(self) -> None:
        if self.reader_task is not None:
            self.reader_task.cancel()
            self.reader_task = None
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

    async def _reader_loop(self) -> None:
        while self.ws is not None:
            try:
                message = await self.ws.recv()
            except Exception:
                self.ws = None
                return

            if isinstance(message, bytes):
                continue

            if message == "2":
                try:
                    await self.ws.send("3")
                except Exception:
                    self.ws = None
                    return

    async def _ensure_connected_locked(self) -> None:
        if self.ws is not None:
            return

        self.ws = await websockets.connect(
            AFA_SOCKET_URL,
            ping_interval=None,
            max_size=max(4096, AFA_WS_MAX_SIZE),
            open_timeout=5,
        )

        try:
            while True:
                message = await asyncio.wait_for(self.ws.recv(), timeout=5)
                if isinstance(message, bytes):
                    continue
                if message == "2":
                    await self.ws.send("3")
                    continue
                if message.startswith("0"):
                    break

            await self.ws.send("40")

            while True:
                message = await asyncio.wait_for(self.ws.recv(), timeout=5)
                if isinstance(message, bytes):
                    continue
                if message == "2":
                    await self.ws.send("3")
                    continue
                if message.startswith("40"):
                    break
        except Exception:
            await self._close_locked()
            raise

        self.reader_task = asyncio.create_task(self._reader_loop())

    async def send(self, event: str, data: Any) -> None:
        packet = "42" + json.dumps([event, data], ensure_ascii=False, separators=(",", ":"))

        async with self.lock:
            try:
                await self._ensure_connected_locked()
                assert self.ws is not None
                await self.ws.send(packet)
            except Exception:
                await self._close_locked()
                raise


afa_socket_relay = AFASocketRelay()


async def maybe_forward_to_afa(payload: dict[str, Any]) -> None:
    global afa_forward_success_count, afa_forward_failure_count, afa_forward_last_error, afa_forward_last_sent_at

    if not afa_forward_enabled():
        return

    try:
        updates = _extract_sensor_updates(payload)

        if updates:
            for update in updates:
                await afa_socket_relay.send("sensor_update", update)
        else:
            relay_payload = _raw_payload_for_afa(payload)
            await afa_socket_relay.send(AFA_SOCKET_EVENT, relay_payload)
        afa_forward_success_count += 1
        afa_forward_last_error = ""
        afa_forward_last_sent_at = datetime.now(timezone.utc).isoformat()
    except Exception as exc:
        afa_forward_failure_count += 1
        afa_forward_last_error = str(exc)


def _next_timestep(device_key: str, sample_ms: int | None) -> int | str:
    if sample_ms is None:
        return ""
    prev = last_sample_ms_by_device.get(device_key)
    last_sample_ms_by_device[device_key] = sample_ms
    if prev is None:
        return ""
    return sample_ms - prev


def _format_hms_ms(total_ms: int) -> str:
    ms = total_ms % 1000
    total_sec = total_ms // 1000
    sec = total_sec % 60
    total_min = total_sec // 60
    minute = total_min % 60
    hour = (total_min // 60) % 24
    return f"{hour:02d}:{minute:02d}:{sec:02d}.{ms:03d}"


def _next_timeline(device_key: str) -> tuple[int, str]:
    idx = sample_index_by_device.get(device_key, 0)
    sample_index_by_device[device_key] = idx + 1
    elapsed_ms = idx * 5  # fixed 5ms grid
    return elapsed_ms, _format_hms_ms(elapsed_ms)


def _timeline_wallclock(device_key: str) -> tuple[int, int, str, str]:
    elapsed_ms, elapsed_hms_ms = _next_timeline(device_key)
    base_ms = logging_started_epoch_ms if logging_started_epoch_ms > 0 else int(datetime.now(timezone.utc).timestamp() * 1000)
    wallclock_epoch_ms = base_ms + elapsed_ms
    wallclock_dt = datetime.fromtimestamp(wallclock_epoch_ms / 1000.0, tz=timezone.utc)
    wallclock_utc_ms = wallclock_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return elapsed_ms, wallclock_epoch_ms, wallclock_utc_ms, elapsed_hms_ms


WIDE_COLUMN_ALIASES = {
    "speed": "speed",
    "velocity": "speed",
    "wheel_rpm": "wheel_rpm",
    "wheel_pulse": "wheel_pulse",
    "wheel_teeth": "wheel_teeth",
    "accelx": "accelX",
    "accely": "accelY",
    "accelz": "accelZ",
    "ax": "accelX",
    "ay": "accelY",
    "az": "accelZ",
    "front_tire": "front_Tire",
    "front_tie": "front_Tire",
    "rear_tire": "rear_Tire",
    "rear_tie": "rear_Tire",
    "accel_p": "accel_p",
    "brake_p": "break_p",
    "break_p": "break_p",
    "brake": "break_p",
    "steering_s": "steering_speed",
    "steering_speed": "steering_speed",
    "steering_a": "steering_angle",
    "steering_angle": "steering_angle",
    "linear_fl": "linear_fl",
    "linear_fr": "linear_fr",
    "linear_rl": "linear_rl",
    "linear_rr": "linear_rr",
    "fr": "linear_fr",
    "fl": "linear_fl",
    "rl": "linear_rl",
    "rr": "linear_rr",
    "ecu_temp": "ECU_temp",
    "x_g": "accelX",
    "y_g": "accelY",
    "z_g": "accelZ",
    "x": "accelX",
    "y": "accelY",
    "z": "accelZ",
    "tp": "tp",
    "hm": "hm",
    "inv_temp_igt": "INV_TEMP_IGT",
    "inv_temp_rtd1": "INV_TEMP_RTD1",
    "inv_temp_rtd2": "INV_TEMP_RTD2",
    "inv_temp_gatedriver": "INV_TEMP_gatedriver",
    "inv_temp_controlboard": "INV_TEMP_controlboard",
    "inv_temp_coolant": "INV_TEMP_coolant",
    "inv_temp_hotspot": "INV_TEMP_hotspot",
    "inv_temp_motor": "INV_TEMP_motor",
    "inv_moter_speed": "INV_moter_speed",
    "inv_motor_speed": "INV_moter_speed",
    "inv_moter_angle": "INV_moter_angle",
    "inv_motor_angle": "INV_moter_angle",
    "inv_dc_currnet": "INV_dc_currnet",
    "inv_dc_current": "INV_dc_currnet",
    "inv_a_currnet": "INV_A_currnet",
    "inv_a_current": "INV_A_currnet",
    "inv_b_currnet": "INV_B_currnet",
    "inv_b_current": "INV_B_currnet",
    "inv_c_currnet": "INV_C_currnet",
    "inv_c_current": "INV_C_currnet",
    "inv_voltage": "INV_voltage",
    "inv_voltage_output": "INV_voltage_output",
    "inv_torque_feedback": "INV_torque_feedback",
    "inv_torque_commanded": "INV_torque_commanded",
    "inv_id_feedback": "INV_id_feedback",
    "inv_iq_feedback": "INV_iq_feedback",
    "bms_charge": "BMS_charge",
    "bms_capacity": "BMS_capacity",
    "bms_voltage": "BMS_voltage",
    "bms_current": "BMS_current",
    "bms_ccl": "BMS_ccl",
    "bms_dcl": "BMS_dcl",
    "bms_temp_maxvalue": "BMS_TEMP_maxvalue",
    "bms_temp_maxid": "BMS_TEMP_maxid",
    "bms_temp_minvalue": "BMS_TEMP_minvalue",
    "bms_temp_minid": "BMS_TEMP_minid",
    "bms_temp_internal": "BMS_TEMP_internal",
}


def _empty_wide_row(payload: dict[str, Any]) -> dict[str, Any]:
    dt = _payload_time_to_datetime(payload)
    return {
        "timestamp": _format_recording_timestamp(dt),
        "stm_time_ns": "",
        "speed": 0,
        "wheel_rpm": 0,
        "wheel_pulse": 0,
        "wheel_teeth": 0,
        "accelX": 0,
        "accelY": 0,
        "accelZ": 0,
        "front_Tire": 0,
        "rear_Tire": 0,
        "accel_p": 0,
        "break_p": 0,
        "steering_speed": 0,
        "steering_angle": 0,
        "linear_fl": 0,
        "linear_fr": 0,
        "linear_rl": 0,
        "linear_rr": 0,
        "ECU_temp": 0,
        "INV_TEMP_IGT": 0,
        "INV_TEMP_RTD1": 0,
        "INV_TEMP_RTD2": 0,
        "INV_TEMP_gatedriver": 0,
        "INV_TEMP_controlboard": 0,
        "INV_TEMP_coolant": 0,
        "INV_TEMP_hotspot": 0,
        "INV_TEMP_motor": 0,
        "INV_moter_speed": 0,
        "INV_moter_angle": 0,
        "INV_dc_currnet": 0,
        "INV_A_currnet": 0,
        "INV_B_currnet": 0,
        "INV_C_currnet": 0,
        "INV_voltage": 0,
        "INV_voltage_output": 0,
        "INV_torque_feedback": 0,
        "INV_torque_commanded": 0,
        "INV_id_feedback": 0,
        "INV_iq_feedback": 0,
        "BMS_charge": 0,
        "BMS_capacity": 0,
        "BMS_voltage": 0,
        "BMS_current": 0,
        "BMS_ccl": 0,
        "BMS_dcl": 0,
        "BMS_TEMP_maxvalue": 0,
        "BMS_TEMP_maxid": 0,
        "BMS_TEMP_minvalue": 0,
        "BMS_TEMP_minid": 0,
        "BMS_TEMP_internal": 0,
        "tp": 0,
        "hm": 0,
    }


def _set_wide_value(row: dict[str, Any], raw_name: str, value: Any) -> None:
    normalized = raw_name.strip().lower().replace("-", "_").replace(".", "_")
    target = WIDE_COLUMN_ALIASES.get(normalized)
    if target is None:
        return
    row[target] = value


def _apply_sensor_map_to_row(row: dict[str, Any], parsed: dict[str, Any]) -> None:
    car = parsed.get("car")
    if isinstance(car, dict):
        _set_wide_value(row, "speed", car.get("speed"))
        _set_wide_value(row, "accel_p", car.get("accel"))
        _set_wide_value(row, "brake", car.get("brake"))

        accel2 = car.get("accel2")
        if isinstance(accel2, dict):
            _set_wide_value(row, "accelx", accel2.get("accel2_x"))
            _set_wide_value(row, "accely", accel2.get("accel2_y"))
            _set_wide_value(row, "accelz", accel2.get("accel2_z"))

        temp = car.get("temp")
        if isinstance(temp, dict):
            _set_wide_value(row, "front_tie", temp.get("front_tie"))
            _set_wide_value(row, "rear_tie", temp.get("rear_tie"))

        steering = car.get("steering")
        if isinstance(steering, dict):
            _set_wide_value(row, "steering_speed", steering.get("speed"))
            _set_wide_value(row, "steering_angle", steering.get("angle"))

        linear = car.get("linear")
        if isinstance(linear, dict):
            _set_wide_value(row, "linear_fl", linear.get("front_left"))
            _set_wide_value(row, "linear_fr", linear.get("front_right"))
            _set_wide_value(row, "linear_rl", linear.get("rear_left"))
            _set_wide_value(row, "linear_rr", linear.get("rear_right"))

    linear = parsed.get("linear")
    if isinstance(linear, dict):
        for key, value in linear.items():
            if key == "fl":
                row["linear_fl"] = value
            elif key == "fr":
                row["linear_fr"] = value
            elif key == "rl":
                row["linear_rl"] = value
            elif key == "rr":
                row["linear_rr"] = value
            else:
                _set_wide_value(row, f"linear_{key}", value)

    mcu_die_temp = _extract_mcu_die_temp(parsed)
    if mcu_die_temp is not None:
        row["ECU_temp"] = mcu_die_temp

    for accel_key in ("accel", "accelerometer", "acceleration"):
        accel = parsed.get(accel_key)
        if isinstance(accel, dict):
            for key, value in accel.items():
                _set_wide_value(row, key, value)

    steering = parsed.get("steering")
    if isinstance(steering, dict):
        for key, value in steering.items():
            _set_wide_value(row, f"steering_{key}", value)

    inverter = parsed.get("inverter")
    if isinstance(inverter, dict):
        temperature = inverter.get("temperature")
        if isinstance(temperature, dict):
            igbt = temperature.get("igbt")
            if isinstance(igbt, dict):
                igbt_max = igbt.get("max")
                if isinstance(igbt_max, dict):
                    _set_wide_value(row, "inv_temp_igt", igbt_max.get("temperature"))
            rtd = temperature.get("rtd")
            if isinstance(rtd, dict):
                _set_wide_value(row, "inv_temp_rtd1", rtd.get("rtd1"))
                _set_wide_value(row, "inv_temp_rtd2", rtd.get("rtd2"))
            _set_wide_value(row, "inv_temp_gatedriver", temperature.get("gatedriver"))
            _set_wide_value(row, "inv_temp_controlboard", temperature.get("controlboard"))
            _set_wide_value(row, "inv_temp_coolant", temperature.get("coolant"))
            _set_wide_value(row, "inv_temp_hotspot", temperature.get("hotspot"))
            _set_wide_value(row, "inv_temp_motor", temperature.get("motor"))

        motor = inverter.get("motor")
        if isinstance(motor, dict):
            _set_wide_value(row, "inv_moter_speed", motor.get("speed"))
            _set_wide_value(row, "inv_moter_angle", motor.get("angle"))

        current = inverter.get("current")
        if isinstance(current, dict):
            _set_wide_value(row, "inv_dc_currnet", current.get("dc_bus"))
            _set_wide_value(row, "inv_a_currnet", current.get("A"))
            _set_wide_value(row, "inv_b_currnet", current.get("B"))
            _set_wide_value(row, "inv_c_currnet", current.get("C"))

        voltage = inverter.get("voltage")
        if isinstance(voltage, dict):
            _set_wide_value(row, "inv_voltage", voltage.get("dc_bus"))
            _set_wide_value(row, "inv_voltage_output", voltage.get("output"))

        torque = inverter.get("torque")
        if isinstance(torque, dict):
            _set_wide_value(row, "inv_torque_feedback", torque.get("feedback"))
            _set_wide_value(row, "inv_torque_commanded", torque.get("commanded"))

        feedback = inverter.get("feedback")
        if isinstance(feedback, dict):
            _set_wide_value(row, "inv_id_feedback", feedback.get("id"))
            _set_wide_value(row, "inv_iq_feedback", feedback.get("iq"))

    bms = parsed.get("bms")
    if isinstance(bms, dict):
        _set_wide_value(row, "bms_charge", bms.get("charge"))
        _set_wide_value(row, "bms_capacity", bms.get("capacity"))
        _set_wide_value(row, "bms_voltage", bms.get("voltage"))
        _set_wide_value(row, "bms_current", bms.get("current"))
        _set_wide_value(row, "bms_ccl", bms.get("ccl"))
        _set_wide_value(row, "bms_dcl", bms.get("dcl"))
        temperature = bms.get("temperature")
        if isinstance(temperature, dict):
            temp_max = temperature.get("max")
            if isinstance(temp_max, dict):
                _set_wide_value(row, "bms_temp_maxvalue", temp_max.get("value"))
                _set_wide_value(row, "bms_temp_maxid", temp_max.get("id"))
            temp_min = temperature.get("min")
            if isinstance(temp_min, dict):
                _set_wide_value(row, "bms_temp_minvalue", temp_min.get("value"))
                _set_wide_value(row, "bms_temp_minid", temp_min.get("id"))
            _set_wide_value(row, "bms_temp_internal", temperature.get("internal"))

    updates = parsed.get("sensor_updates")
    if isinstance(updates, list):
        for item in updates:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            if isinstance(path, str) and "value" in item:
                _set_wide_value(row, path.split(".")[-1], item.get("value"))

    path = parsed.get("path")
    if isinstance(path, str) and "value" in parsed:
        _set_wide_value(row, path.split(".")[-1], parsed.get("value"))

    for key, value in parsed.items():
        if key in {"device", "d", "s", "t", "ts", "samples", "linear", "accel", "accelerometer", "acceleration", "sensor_updates", "path", "value", "steering", "inverter", "bms", "car", "ecu", "mcu"}:
            continue
        if isinstance(value, (dict, list)):
            continue
        _set_wide_value(row, key, value)


def _row_with_timeline(payload: dict[str, Any], device_key: str) -> dict[str, Any]:
    row = _empty_wide_row(payload)
    # 실제 로깅 시각(서버 수신 시각)을 timestamp에 기록
    # STM 샘플 시각(t/ts)은 build_log_rows에서 stm_time_ns에 기록
    row["timestamp"] = _format_recording_timestamp(_payload_time_to_datetime(payload))
    row["stm_time_ns"] = ""
    return row


def build_log_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    message = payload.get("message", "")

    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        return [_empty_wide_row(payload)]

    if isinstance(parsed, dict):
        device = parsed.get("device") or parsed.get("d") or ""
        device_key = str(device or f"{payload.get('ip', '')}:{payload.get('port', '')}")
        samples = parsed.get("samples")
        if isinstance(samples, list) and samples:
            rows: list[dict[str, Any]] = []
            for s in samples:
                if isinstance(s, dict):
                    row = _row_with_timeline(payload, device_key)
                    row["stm_time_ns"] = _extract_stm_time_ns(s)
                    _apply_sensor_map_to_row(row, s)
                    rows.append(row)
                else:
                    row = _row_with_timeline(payload, device_key)
                    rows.append(row)
            return rows
        row = _row_with_timeline(payload, device_key)
        row["stm_time_ns"] = _extract_stm_time_ns(parsed)
        sample_ms = _as_int(parsed.get("t"))
        _next_timestep(device_key, sample_ms)
        _apply_sensor_map_to_row(row, parsed)
        return [row]

    return [_empty_wide_row(payload)]


def queue_log_rows(payload: dict[str, Any]) -> None:
    session = logging_session
    if not logging_enabled or session is None:
        return
    session.enqueue_rows(build_log_rows(payload))


def _discard_live_task(task: asyncio.Task[Any]) -> None:
    live_tasks.discard(task)
    try:
        task.result()
    except Exception:
        pass


def schedule_live_processing(payload: dict[str, Any], *, skip_forward: bool = False) -> None:
    global live_tasks_dropped
    if len(live_tasks) >= LIVE_TASK_MAX:
        live_tasks_dropped += 1
        return
    task = asyncio.create_task(process_live_payload(payload, skip_forward=skip_forward))
    live_tasks.add(task)
    task.add_done_callback(_discard_live_task)


class UDPProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        message = data.decode("utf-8", errors="replace").strip()
        record_traffic(parse_sample_count(message))
        payload: dict[str, Any] = {
            "time": datetime.now(timezone.utc).isoformat(),
            "ip": addr[0],
            "port": addr[1],
            "message": message,
            "transport": "udp",
        }
        mark_device_seen(payload)
        record_platform_traffic(payload, parse_sample_count(message))
        update_latest_sensor_snapshot(payload)
        payload = apply_offsets_to_payload(payload)
        payload = apply_wheel_speed_to_payload(payload)
        queue_log_rows(payload)
        schedule_live_processing(payload)


async def process_live_payload(payload: dict[str, Any], *, skip_forward: bool = False) -> None:
    await broadcast_packet(payload)
    await maybe_forward_payload(payload, skip=skip_forward)
    await maybe_forward_to_afa(payload)


async def broadcast_packet(payload: dict[str, Any]) -> None:
    dead_clients: list[WebSocket] = []

    device_key = _device_key_from_payload(payload)

    broadcast_payload = dict(payload)
    if device_key in device_offsets and device_offsets[device_key]:
        broadcast_payload["_offsets"] = device_offsets[device_key]

    text_payload = json.dumps(broadcast_payload, ensure_ascii=False)

    for client in ws_clients:
        try:
            await client.send_text(text_payload)
        except Exception:
            dead_clients.append(client)

    for client in dead_clients:
        ws_clients.discard(client)


async def sim_linear_loop() -> None:
    global sim_linear_packets

    interval_sec = max(SIM_LINEAR_INTERVAL_MS, 20) / 1000.0
    while True:
        payload = build_sim_linear_payload()
        record_traffic(4)
        mark_device_seen(payload)
        update_latest_sensor_snapshot(payload)
        payload = apply_offsets_to_payload(payload)
        queue_log_rows(payload)
        schedule_live_processing(payload)
        sim_linear_packets += 1
        await asyncio.sleep(interval_sec)


async def send_afa_linear_demo() -> dict[str, Any]:
    payload = build_sim_linear_payload()
    updates = _extract_sensor_updates(payload)
    sent = 0
    last_exception = ""

    for update in updates:
        try:
            await afa_socket_relay.send("sensor_update", update)
            sent += 1
        except Exception as exc:
            last_exception = str(exc)
            break

    return {
        "ok": sent == len(updates) and sent > 0,
        "sent": sent,
        "updates": updates,
        "last_exception": last_exception,
    }


@app.on_event("startup")
async def startup_event() -> None:
    global forward_queue
    deleted = cleanup_log_dir()
    if deleted:
        print(f"[logging] startup cleanup removed {len(deleted)} log file(s)")
    recovered_logs = _recover_orphan_log_files()
    if recovered_logs:
        print(f"[logging] recovered {recovered_logs} log file(s) from {LOG_DIR}")
    loop = asyncio.get_running_loop()
    forward_queue = asyncio.Queue(maxsize=max(1, FORWARD_QUEUE_MAX))
    app.state.udp_transport = None
    app.state.tcp_server = None
    app.state.forward_worker_task = None
    if ENABLE_UDP:
        udp_transport, _ = await loop.create_datagram_endpoint(
            lambda: UDPProtocol(),
            local_addr=(UDP_BIND_HOST, UDP_BIND_PORT),
        )
        app.state.udp_transport = udp_transport
    if ENABLE_TCP:
        app.state.tcp_server = await asyncio.start_server(
            handle_tcp_client,
            host=TCP_BIND_HOST,
            port=TCP_BIND_PORT,
        )
    if forward_enabled():
        app.state.forward_worker_task = asyncio.create_task(forward_worker())
    if SIM_LINEAR_ENABLED:
        app.state.sim_linear_task = asyncio.create_task(sim_linear_loop())


@app.on_event("shutdown")
async def shutdown_event() -> None:
    global logging_enabled, logging_session, forward_queue
    udp_transport = getattr(app.state, "udp_transport", None)
    if udp_transport is not None:
        udp_transport.close()
    tcp_server = getattr(app.state, "tcp_server", None)
    if tcp_server is not None:
        tcp_server.close()
        await tcp_server.wait_closed()
    sim_linear_task = getattr(app.state, "sim_linear_task", None)
    if sim_linear_task is not None:
        sim_linear_task.cancel()
        try:
            await sim_linear_task
        except asyncio.CancelledError:
            pass
    forward_worker_task = getattr(app.state, "forward_worker_task", None)
    if forward_worker_task is not None:
        forward_worker_task.cancel()
        try:
            await forward_worker_task
        except asyncio.CancelledError:
            pass
    logging_enabled = False
    session = logging_session
    logging_session = None
    if session is not None:
        await session.close()
    if live_tasks:
        await asyncio.gather(*list(live_tasks), return_exceptions=True)
    async with afa_socket_relay.lock:
        await afa_socket_relay._close_locked()
    forward_queue = None


@app.get("/")
def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"request": request, **nav_context("logger", request)},
    )


@app.post("/ingest")
async def ingest_http(request: Request) -> dict[str, Any]:
    body = await request.json()
    message = json.dumps(body, ensure_ascii=False)
    sample_count = 1
    if isinstance(body, dict):
        samples = body.get("samples")
        if isinstance(samples, list):
            sample_count = max(1, len(samples))
    record_traffic(sample_count)

    client = request.client
    ip = client.host if client else "unknown"
    port = client.port if client else 0
    payload: dict[str, Any] = {
        "time": datetime.now(timezone.utc).isoformat(),
        "ip": ip,
        "port": port,
        "message": message,
        "transport": "http",
    }
    skip_forward = request.headers.get(FORWARD_HEADER) == "1"
    mark_device_seen(payload)
    record_platform_traffic(payload, sample_count)
    update_latest_sensor_snapshot(payload)
    payload = apply_offsets_to_payload(payload)
    payload = apply_wheel_speed_to_payload(payload)
    queue_log_rows(payload)
    schedule_live_processing(payload, skip_forward=skip_forward)
    return {"ok": True, "samples": sample_count}


@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket) -> None:
    await websocket.accept()
    ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_clients.discard(websocket)


async def handle_tcp_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    peer = writer.get_extra_info("peername")
    ip = peer[0] if isinstance(peer, tuple) else "unknown"
    port = peer[1] if isinstance(peer, tuple) and len(peer) > 1 else 0
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            message = line.decode("utf-8", errors="replace").strip()
            if not message:
                continue
            record_traffic(parse_sample_count(message))
            payload: dict[str, Any] = {
                "time": datetime.now(timezone.utc).isoformat(),
                "ip": ip,
                "port": port,
                "message": message,
                "transport": "tcp",
            }
            mark_device_seen(payload)
            record_platform_traffic(payload, parse_sample_count(message))
            update_latest_sensor_snapshot(payload)
            payload = apply_offsets_to_payload(payload)
            payload = apply_wheel_speed_to_payload(payload)
            queue_log_rows(payload)
            schedule_live_processing(payload)
    finally:
        writer.close()
        await writer.wait_closed()


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    session_status = logging_session.status() if logging_session is not None else None
    return {
        "total_packets": total_packets,
        "total_samples": total_samples,
        "hz_1s": hz_last(1.0),
        "hz_5s_avg": round(hz_last(5.0) / 5.0, 1),
        "device_hz": {
            "esp32": {
                "hz_1s": hz_last_for_platform("esp32", 1.0),
                "hz_5s_avg": round(hz_last_for_platform("esp32", 5.0) / 5.0, 1),
            },
            "raspberry_pi": {
                "hz_1s": hz_last_for_platform("raspberry_pi", 1.0),
                "hz_5s_avg": round(hz_last_for_platform("raspberry_pi", 5.0) / 5.0, 1),
            },
        },
        "forward_enabled": forward_enabled(),
        "forward_url": MONITOR_FORWARD_URL,
        "forward_success_count": forward_success_count,
        "forward_failure_count": forward_failure_count,
        "forward_dropped_count": forward_dropped_count,
        "forward_last_error": forward_last_error,
        "forward_last_sent_at": forward_last_sent_at,
        "forward_queue_max": FORWARD_QUEUE_MAX,
        "forward_queue_size": forward_queue.qsize() if forward_queue is not None else 0,
        "live_task_max": LIVE_TASK_MAX,
        "live_tasks_active": len(live_tasks),
        "live_tasks_dropped": live_tasks_dropped,
        "log_queue_max_rows": LOG_QUEUE_MAX_ROWS,
        "device_cache_max": DEVICE_CACHE_MAX,
        "afa_forward_enabled": afa_forward_enabled(),
        "afa_socket_url": AFA_SOCKET_URL,
        "afa_socket_event": AFA_SOCKET_EVENT,
        "afa_forward_success_count": afa_forward_success_count,
        "afa_forward_failure_count": afa_forward_failure_count,
        "afa_forward_last_error": afa_forward_last_error,
        "afa_forward_last_sent_at": afa_forward_last_sent_at,
        "sim_linear_enabled": SIM_LINEAR_ENABLED,
        "sim_linear_interval_ms": SIM_LINEAR_INTERVAL_MS,
        "sim_linear_packets": sim_linear_packets,
        "offset_device_count": len(device_offsets),
        "snapshot_device_count": len(latest_sensor_snapshot_by_device),
        "wheel_teeth_count": _wheel_teeth_count(),
        "wheel_sample_period_ms": _wheel_sample_period_ms(),
        "logging_enabled": logging_enabled,
        "logging_session": session_status,
        "enable_udp": ENABLE_UDP,
        "enable_tcp": ENABLE_TCP,
        "udp_bind_host": UDP_BIND_HOST,
        "udp_bind_port": UDP_BIND_PORT,
        "tcp_bind_host": TCP_BIND_HOST,
        "tcp_bind_port": TCP_BIND_PORT,
    }


@app.post("/api/relay/afa-test")
async def relay_afa_test() -> dict[str, Any]:
    result = await send_afa_linear_demo()
    return {
        **result,
        "afa_forward_enabled": afa_forward_enabled(),
        "afa_socket_url": AFA_SOCKET_URL,
        "afa_socket_event": AFA_SOCKET_EVENT,
        "afa_forward_success_count": afa_forward_success_count,
        "afa_forward_failure_count": afa_forward_failure_count,
        "afa_forward_last_error": afa_forward_last_error,
        "afa_forward_last_sent_at": afa_forward_last_sent_at,
    }


@app.get("/api/logging/status")
def logging_status() -> dict[str, Any]:
    connected_devices = online_device_count()
    session_status = logging_session.status() if logging_session is not None else {}
    return {
        "enabled": logging_enabled,
        "count": session_status.get("rows_written", 0),
        "connected_devices": connected_devices,
        "can_start": connected_devices > 0,
        "rows_written": session_status.get("rows_written", 0),
        "rows_enqueued": session_status.get("rows_enqueued", 0),
        "rows_pending": session_status.get("rows_pending", 0),
        "dropped_rows": session_status.get("dropped_rows", 0),
        "file_count": session_status.get("file_count", 0),
        "part_number": session_status.get("part_number", 0),
        "active_file": session_status.get("active_file", ""),
        "files": session_status.get("files", []),
        "session_id": session_status.get("session_id", ""),
        "output_dir": str(LOG_DIR),
        "file_bytes": session_status.get("file_bytes", 0),
        "compressed": session_status.get("compressed", LOG_DOWNLOAD_GZIP),
        "rolling_enabled": bool(session_status.get("rolling_enabled")) if session_status else (LOG_ROLL_SECONDS > 0 or LOG_ROLL_SIZE_BYTES > 0),
        "roll_seconds": session_status.get("roll_seconds", LOG_ROLL_SECONDS),
        "roll_size_bytes": session_status.get("roll_size_bytes", LOG_ROLL_SIZE_BYTES),
        "last_error": session_status.get("last_error", ""),
        "completed_sessions": _completed_sessions_public(),
        "wheel_config": {
            "teeth_count": _wheel_teeth_count(),
            "sample_period_ms": _wheel_sample_period_ms(),
        },
        "disk": _disk_usage(LOG_DIR),
        "disk_min_free_bytes": LOG_MIN_FREE_BYTES,
        "disk_ok": _disk_free_ok(LOG_DIR)[0],
        "camera_recording": _linked_camera_recording_status() if logging_enabled else {"linked": LINK_CAMERA_RECORDING},
    }


@app.post("/api/logging/start")
async def logging_start() -> Response:
    global logging_enabled, logging_session, last_sample_ms_by_device, sample_index_by_device, logging_started_epoch_ms, wheel_last_sample_ms_by_device
    connected_devices = online_device_count()
    if connected_devices < 1:
        return Response(
            content=json.dumps({"ok": False, "message": "연결된 장비가 없어 로깅을 시작할 수 없습니다.", "connected_devices": 0}, ensure_ascii=False),
            media_type="application/json",
            status_code=409,
        )

    if logging_enabled:
        session_status = logging_session.status() if logging_session is not None else {}
        return Response(
            content=json.dumps({"ok": True, "enabled": True, "count": session_status.get("rows_written", 0), "connected_devices": connected_devices}, ensure_ascii=False),
            media_type="application/json",
            status_code=200,
        )

    disk_ok, disk_stats = _disk_free_ok(LOG_DIR)
    if not disk_ok:
        cleanup_log_dir()
        disk_ok, disk_stats = _disk_free_ok(LOG_DIR)
    if not disk_ok:
        free_mb = disk_stats["free_bytes"] // (1024 * 1024)
        need_mb = LOG_MIN_FREE_BYTES // (1024 * 1024)
        return Response(
            content=json.dumps(
                {
                    "ok": False,
                    "message": (
                        f"디스크 여유 공간이 부족해 로깅을 시작할 수 없습니다 "
                        f"({free_mb}MB 남음, 최소 {need_mb}MB 필요). "
                        "서버 용량을 확보한 뒤 다시 시도하세요."
                    ),
                    "disk": disk_stats,
                    "connected_devices": connected_devices,
                },
                ensure_ascii=False,
            ),
            media_type="application/json",
            status_code=507,
        )

    cleanup_log_dir()
    last_sample_ms_by_device = {}
    sample_index_by_device = {}
    wheel_last_sample_ms_by_device = {}
    logging_started_epoch_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    logging_session = CsvLogSession(started_epoch_ms=logging_started_epoch_ms)
    logging_session.start()
    logging_enabled = True
    camera_result = _start_linked_camera_recording(logging_session.session_id)
    sync_path = _write_session_sync(
        logging_session.session_id,
        {
            "logging_started_at": datetime.fromtimestamp(
                logging_started_epoch_ms / 1000.0, tz=timezone.utc
            ).isoformat(),
            "logging_started_epoch_ms": logging_started_epoch_ms,
            "csv_prefix": f"sensor_log_{logging_session.session_id}",
            "camera_prefix": f"camera-{logging_session.session_id}",
            "camera_mode": str(camera_result.get("record_mode") or camera_result.get("mode") or ""),
            "camera_started_at": str(camera_result.get("started_at") or ""),
            "camera_filename": str(camera_result.get("filename") or ""),
            "camera_ok": bool(camera_result.get("ok")),
            "offset_sec_hint": 0.0,
            "sync_rule": "video_t0_equals_logging_start",
            "note": "분석 시 CSV t0(첫 timestamp)와 영상 0초를 맞추고, session_sync의 offset_sec_hint로 미세 보정",
        },
    )
    return Response(
        content=json.dumps(
            {
                "ok": True,
                "enabled": logging_enabled,
                "count": 0,
                "connected_devices": connected_devices,
                "output_dir": str(LOG_DIR),
                "compressed": LOG_DOWNLOAD_GZIP,
                "session_id": logging_session.session_id,
                "camera_recording": camera_result,
                "session_sync": str(sync_path),
            },
            ensure_ascii=False,
        ),
        media_type="application/json",
        status_code=200,
    )


@app.post("/api/logging/stop")
async def logging_stop() -> Response:
    global logging_enabled, logging_session
    session = logging_session
    logging_enabled = False
    logging_session = None

    if session is None:
        camera_result = _stop_linked_camera_recording()
        return Response(
            content=json.dumps(
                {"ok": False, "message": "저장할 데이터가 없습니다.", "camera_recording": camera_result},
                ensure_ascii=False,
            ),
            media_type="application/json",
            status_code=200,
        )

    await session.close()
    camera_result = _stop_linked_camera_recording()
    if session.rows_written < 1:
        return Response(
            content=json.dumps(
                {
                    "ok": False,
                    "message": "저장할 데이터가 없습니다.",
                    "camera_recording": camera_result,
                },
                ensure_ascii=False,
            ),
            media_type="application/json",
            status_code=200,
        )
    download_token = secrets.token_urlsafe(16)
    download_payload = session.build_download_payload()
    _store_completed_download(download_token, download_payload)
    sync_path = _write_session_sync(
        session.session_id,
        {
            "logging_stopped_at": datetime.now(timezone.utc).isoformat(),
            "csv_filename": download_payload.get("filename", ""),
            "csv_rows": session.rows_written,
            "camera_filename": str(camera_result.get("filename") or ""),
            "camera_segments": list(camera_result.get("segments") or camera_result.get("recording", {}).get("segments") or []),
            "camera_bytes": int(
                camera_result.get("file_size")
                or camera_result.get("recording", {}).get("file_size")
                or camera_result.get("bytes_written")
                or 0
            ),
            "offset_sec_hint": 0.0,
            "sync_rule": "video_t0_equals_logging_start",
        },
    )
    return Response(
        content=json.dumps(
            {
                "ok": True,
                "download_token": download_token,
                "download_url": f"/api/logging/download/{download_token}",
                "filename": download_payload["filename"],
                "compressed": download_payload["compressed"],
                "download_bytes": download_payload["download_bytes"],
                "part_count": download_payload.get("part_count", 1),
                "is_archive": download_payload.get("is_archive", False),
                "completed_sessions": _completed_sessions_public(),
                "session_id": session.session_id,
                "camera_recording": camera_result,
                "session_sync": str(sync_path),
            },
            ensure_ascii=False,
        ),
        media_type="application/json",
        status_code=200,
    )


@app.get("/api/logging/download/{download_token}")
def logging_download(download_token: str) -> Response:
    payload = completed_downloads.get(download_token)
    if payload is None:
        return Response(
            content=json.dumps({"ok": False, "message": "다운로드 가능한 파일이 없습니다. 다시 로깅을 시도해주세요."}, ensure_ascii=False),
            media_type="application/json",
            status_code=404,
        )

    file_path = Path(payload["file_path"])
    if not file_path.is_file():
        return Response(
            content=json.dumps({"ok": False, "message": "다운로드 파일을 찾을 수 없습니다."}, ensure_ascii=False),
            media_type="application/json",
            status_code=404,
        )

    return FileResponse(
        path=file_path,
        media_type=(
            "application/zip"
            if payload.get("is_archive")
            else ("application/gzip" if payload.get("compressed") else "text/csv; charset=utf-8")
        ),
        filename=payload["filename"],
        headers={"X-Download-Bytes": str(payload.get("download_bytes", file_path.stat().st_size))},
    )


@app.delete("/api/logging/session/{download_token}")
def logging_delete_session(download_token: str) -> dict[str, Any]:
    payload = completed_downloads.pop(download_token, None)
    if payload is None:
        return {"ok": False, "message": "삭제할 로깅 세션이 없습니다."}

    _delete_download_payload(payload)
    return {
        "ok": True,
        "deleted_token": download_token,
        "completed_sessions": _completed_sessions_public(),
    }


@app.post("/api/offset/calibrate")
async def offset_calibrate(request: Request) -> dict[str, Any]:
    global device_offsets
    try:
        body = await request.json()
    except Exception:
        body = {}

    if not isinstance(body, dict):
        body = {}

    device_key = str(body.get("device", "")).strip()
    sensor_values = body.get("sensor_values")

    if isinstance(sensor_values, dict) and sensor_values:
        if not device_key:
            return {"ok": False, "message": "sensor_values를 직접 지정할 때는 device가 필요합니다."}
        device_offsets[device_key] = {}
        for sensor_path, value in sensor_values.items():
            value_f = _as_float(value)
            if value_f is None:
                continue
            path = str(sensor_path)
            device_offsets[device_key][path] = raw_value_to_offset(path, value_f)
        from offset_shared import public_offset_status

        status = public_offset_status(device_offsets)
        return {
            "ok": True,
            "mode": "manual",
            "device": device_key,
            "offsets": device_offsets.get(device_key, {}),
            **status,
        }

    if device_key:
        snapshot = latest_sensor_snapshot_by_device.get(device_key)
        if not snapshot:
            return {"ok": False, "message": f"{device_key}의 최신 센서 데이터가 없어 오프셋을 잡을 수 없습니다."}
        device_offsets[device_key] = snapshot_to_offsets(snapshot)
        from offset_shared import public_offset_status

        return {
            "ok": True,
            "mode": "capture_latest",
            "device": device_key,
            "offsets": device_offsets[device_key],
            **public_offset_status(device_offsets),
        }

    if not latest_sensor_snapshot_by_device:
        return {"ok": False, "message": "최신 센서 데이터가 없어 오프셋을 잡을 수 없습니다."}

    calibrated_devices: list[str] = []
    for key, snapshot in latest_sensor_snapshot_by_device.items():
        if not snapshot:
            continue
        device_offsets[key] = snapshot_to_offsets(snapshot)
        calibrated_devices.append(key)

    if not calibrated_devices:
        return {"ok": False, "message": "캘리브레이션 가능한 센서 데이터가 없습니다."}

    from offset_shared import public_offset_status

    return {
        "ok": True,
        "mode": "capture_latest_all",
        "calibrated_devices": calibrated_devices,
        "count": len(calibrated_devices),
        **public_offset_status(device_offsets),
    }


@app.get("/api/offset/status")
def offset_status() -> dict[str, Any]:
    from offset_shared import public_offset_status

    return {
        "ok": True,
        **public_offset_status(device_offsets),
        "snapshot_device_count": len(latest_sensor_snapshot_by_device),
        "latest_snapshots": latest_sensor_snapshot_by_device,
    }


@app.get("/api/wheel-config/status")
def wheel_config_status() -> dict[str, Any]:
    return {
        "ok": True,
        "teeth_count": _wheel_teeth_count(),
        "sample_period_ms": _wheel_sample_period_ms(),
    }


@app.post("/api/wheel-config")
async def wheel_config_update(request: Request) -> dict[str, Any]:
    global wheel_config, wheel_last_sample_ms_by_device
    try:
        body = await request.json()
    except Exception:
        body = {}

    if not isinstance(body, dict):
        body = {}

    teeth_count = _as_int(body.get("teeth_count"))
    if teeth_count is None or teeth_count < 1:
        return {"ok": False, "message": "톤 휠 톱니 수는 1 이상의 정수여야 합니다."}

    sample_period_ms = _as_float(body.get("sample_period_ms"))
    wheel_config["teeth_count"] = teeth_count
    if sample_period_ms is not None and sample_period_ms > 0:
        wheel_config["sample_period_ms"] = sample_period_ms
    wheel_last_sample_ms_by_device = {}

    return {
        "ok": True,
        "message": f"톤 휠 톱니 수를 {teeth_count}개로 저장했습니다.",
        "teeth_count": _wheel_teeth_count(),
        "sample_period_ms": _wheel_sample_period_ms(),
    }


@app.post("/api/wheel-config/reset")
async def wheel_config_reset() -> dict[str, Any]:
    global wheel_config, wheel_last_sample_ms_by_device
    wheel_config["teeth_count"] = 0
    wheel_last_sample_ms_by_device = {}
    return {
        "ok": True,
        "message": "톤 휠 설정을 초기화했습니다.",
        "teeth_count": _wheel_teeth_count(),
        "sample_period_ms": _wheel_sample_period_ms(),
    }


@app.post("/api/offset/reset")
async def offset_reset(request: Request) -> dict[str, Any]:
    global device_offsets
    try:
        body = await request.json()
    except Exception:
        body = {}

    device_key = body.get("device", "").strip() if isinstance(body, dict) else ""

    if device_key:
        if device_key in device_offsets:
            del device_offsets[device_key]
        return {
            "ok": True,
            "device": device_key,
            "message": f"{device_key}의 오프셋이 초기화되었습니다.",
        }

    device_offsets.clear()
    return {
        "ok": True,
        "message": "모든 오프셋이 초기화되었습니다.",
    }