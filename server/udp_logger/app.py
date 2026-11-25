import asyncio
import csv
import gzip
import json
import math
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
from fastapi import BackgroundTasks, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.templating import Jinja2Templates

from offset_shared import (
    apply_offsets_to_parsed,
    extract_offset_candidates,
    merge_device_offsets,
    raw_value_to_offset,
    sensor_offset_label,
    sensor_offset_unit,
    snapshot_to_offsets,
)
from vehicle_speed import motor_rpm_to_speed_kmh
from afa_auth import install_auth
from nav_config import nav_context
from ingest_v2 import IngestV2Store, V2ValidationError, validate_v2_payload
from sync_timeline import rtp_to_ntp_ns, video_pts_to_pi_mono_ns
from log_downsample import downsample_rows
from camera_sync_store import (
    CameraSyncStore,
    PendingCommandExists,
    SyncReportError,
)

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
last_traffic_mono: float = 0.0
last_traffic_mono_by_platform: dict[str, float] = {
    "esp32": 0.0,
    "raspberry_pi": 0.0,
}
total_packets = 0
total_samples = 0
logging_enabled = False
last_sample_ms_by_device: dict[str, int] = {}
sample_index_by_device: dict[str, int] = {}
logging_started_epoch_ms = 0
log_last_stm_ms_by_device: dict[str, float] = {}
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
FORWARD_QUEUE_MAX = int(os.getenv("FORWARD_QUEUE_MAX", "1"))
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
# CSV 기록 목표 Hz (수신이 200Hz여도 stm_ms 기준으로 이 간격만 저장)
LOG_TARGET_HZ = max(1.0, float(os.getenv("LOG_TARGET_HZ", "100")))
LOG_MIN_INTERVAL_MS = 1000.0 / LOG_TARGET_HZ
# 샘플 지터 허용치(ms). 간격이 (목표 간격 - 허용치) 이상이면 저장한다.
# 허용치가 0이면 1 ms 늦은 샘플 다음의 정상 샘플(간격 9 ms)이 버려진다.
LOG_JITTER_TOLERANCE_MS = max(
    0.0, float(os.getenv("LOG_JITTER_TOLERANCE_MS", str(0.2 * LOG_MIN_INTERVAL_MS)))
)
CAMERA_INTERNAL_URL = os.getenv("CAMERA_INTERNAL_URL", "http://127.0.0.1:8012").strip().rstrip("/")
LINK_CAMERA_RECORDING = os.getenv("LINK_CAMERA_RECORDING", "1").strip().lower() in {"1", "true", "yes", "on"}
CAMERA_API_TIMEOUT_SEC = float(os.getenv("CAMERA_API_TIMEOUT_SEC", "3.0"))
# RTSP finalize는 수 초~수 분 걸릴 수 있음. stop만 별도 긴 타임아웃.
CAMERA_STOP_TIMEOUT_SEC = float(os.getenv("CAMERA_STOP_TIMEOUT_SEC", "180"))
CAMERA_STOP_RETRIES = max(1, int(os.getenv("CAMERA_STOP_RETRIES", "3")))
CAMERA_SYNC_SERVER_TOKEN = os.getenv("CAMERA_SYNC_SERVER_TOKEN", "").strip()
CAMERA_SYNC_DEVICE = (
    os.getenv("CAMERA_SYNC_DEVICE", "raspberry-pi-camera").strip()
    or "raspberry-pi-camera"
)
INGEST_DEDUP_RETENTION_DAYS = max(1, int(os.getenv("INGEST_DEDUP_RETENTION_DAYS", "30")))
INGEST_V2_DB_PATH = Path(
    os.getenv("INGEST_V2_DB_PATH", str(LOG_DIR / "ingest_v2.sqlite3"))
)
ingest_v2_store = IngestV2Store(
    INGEST_V2_DB_PATH,
    retention_days=INGEST_DEDUP_RETENTION_DAYS,
)
camera_sync_store = CameraSyncStore(INGEST_V2_DB_PATH)
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
latest_wide_sample_by_device: dict[str, dict[str, Any]] = {}
latest_wide_fields_by_device: dict[str, set[str]] = {}
latest_stm_state_by_device: dict[str, dict[str, float]] = {}
offset_sample_history_by_device: dict[str, deque[tuple[float, dict[str, float]]]] = {}
forward_queue: "asyncio.Queue[dict[str, Any]] | None" = None
LOG_FIELDNAMES = [
    "timestamp",
    "stm_ms",
    "schema_version",
    "pi_boot_id",
    "sender_instance_id",
    "batch_seq",
    "uart_rx_mono_ns",
    "pi_clock_source",
    "stm_epoch",
    "stm_wrap_count",
    "stm_extended_ms",
    "pi_mono_fixed_5ms_ns",
    "pi_mono_est_ns",
    "FR",
    "FL",
    "RR",
    "RL",
    "x_g",
    "y_g",
    "z_g",
    "ecu_temp",
    "steering_angle",
    "steering_speed",
    "inv_temp_igt",
    "inv_temp_motor",
    "inv_motor_speed",
    "vehicle_speed_kmh",
    "inv_dc_current",
    "inv_voltage",
    "motor_precharge",
    "motor_main_contactor",
    "motor_inverter_mode",
    "INV_shudder_torque",
    "INV_id_feedback",
    "INV_iq_feedback",
    "INV_torque_commanded",
    "INV_torque_feedback",
    "INV_id_command",
    "INV_iq_command",
    "bms_charge",
    "bms_voltage",
    "bms_current",
    "bms_ccl",
    "bms_dcl",
    "bms_temp_maxvalue",
    "bms_capacity",
    "can1",
    "can2",
]
V2_TIMING_FIELDNAMES = {
    "schema_version",
    "pi_boot_id",
    "sender_instance_id",
    "batch_seq",
    "uart_rx_mono_ns",
    "pi_clock_source",
    "stm_epoch",
    "stm_wrap_count",
    "stm_extended_ms",
    "pi_mono_fixed_5ms_ns",
    "pi_mono_est_ns",
}
REPORT_NUMERIC_FIELDS = [
    field
    for field in LOG_FIELDNAMES
    if field not in {
        "timestamp",
        "schema_version",
        "pi_boot_id",
        "sender_instance_id",
        "batch_seq",
        "uart_rx_mono_ns",
        "pi_clock_source",
        "stm_epoch",
        "stm_wrap_count",
        "stm_extended_ms",
        "pi_mono_fixed_5ms_ns",
        "pi_mono_est_ns",
        "motor_precharge",
        "motor_main_contactor",
        "motor_inverter_mode",
        "can1",
        "can2",
    }
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


_LOG_LABEL_BAD_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_LOG_LABEL_SPACE_RE = re.compile(r"[\s]+")
_LOG_LABEL_MAX_LEN = 40


def _sanitize_log_label(raw: str | None) -> str:
    """파일명에 붙일 사용자 이름. 비우면 ''. 경로문자·위험문자 제거."""
    text = str(raw or "").strip()
    if not text:
        return ""
    text = _LOG_LABEL_BAD_RE.sub("", text)
    text = _LOG_LABEL_SPACE_RE.sub("_", text)
    text = text.strip("._ ")
    if len(text) > _LOG_LABEL_MAX_LEN:
        text = text[:_LOG_LABEL_MAX_LEN].rstrip("._ ")
    return text


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
                "report": payload.get("report", {}),
            }
        )
    sessions.sort(key=lambda item: item.get("stopped_at", ""), reverse=True)
    return sessions


_LOG_STAMP_RE = re.compile(r"^sensor_log_(\d{8}_\d{6})")
_LOG_DISPLAY_RE = re.compile(
    r"^sensor_log_(\d{8}_\d{6}(?:_\d{6})?)(?:_(.+?))?(?:_part(\d+))?\.csv(\.gz)?$"
)
_LOCAL_TZ = ZoneInfo("Asia/Seoul")


def _normalize_ymd(year: int | str, month: int | str, day: int | str) -> str | None:
    try:
        y = int(year)
        m = int(month)
        d = int(day)
        datetime(y, m, d)
    except (TypeError, ValueError):
        return None
    return f"{y:04d}{m:02d}{d:02d}"


def _local_ymd_from_log_name(name: str) -> str | None:
    match = _LOG_STAMP_RE.match(name)
    if not match:
        return None
    try:
        utc_dt = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return utc_dt.astimezone(_LOCAL_TZ).strftime("%Y%m%d")


def _parse_log_display(name: str) -> dict[str, Any]:
    match = _LOG_DISPLAY_RE.match(name)
    if not match:
        return {"stamp": "", "label": "", "part": ""}
    return {
        "stamp": match.group(1) or "",
        "label": match.group(2) or "",
        "part": match.group(3) or "",
    }


def _safe_log_download_path(filename: str) -> Path | None:
    name = Path(str(filename or "")).name
    if name != str(filename or "").strip():
        return None
    if not name.startswith("sensor_log_"):
        return None
    if not (name.endswith(".csv.gz") or name.endswith(".csv")):
        return None
    if any(ch in name for ch in ("..", "/", "\\")):
        return None
    path = (LOG_DIR / name).resolve()
    try:
        path.relative_to(LOG_DIR.resolve())
    except ValueError:
        return None
    if not path.is_file():
        return None
    return path


def _iter_downloadable_log_files() -> list[Path]:
    if not LOG_DIR.is_dir():
        return []
    active = _active_log_paths()
    gz_paths: list[Path] = []
    csv_paths: list[Path] = []
    for path in LOG_DIR.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if not name.startswith("sensor_log_"):
            continue
        if ".bak" in name or name.endswith(".zip"):
            continue
        if str(path.resolve()) in active:
            continue
        if name.endswith(".csv.gz"):
            gz_paths.append(path)
        elif name.endswith(".csv"):
            csv_paths.append(path)
    gz_stems = {p.name[:-3] for p in gz_paths}  # foo.csv.gz -> foo.csv
    files = list(gz_paths)
    for path in csv_paths:
        if path.name not in gz_stems and not (LOG_DIR / f"{path.name}.gz").exists():
            files.append(path)
    files.sort(key=lambda item: item.name, reverse=True)
    return files


def _logging_files_calendar() -> dict[str, Any]:
    days: dict[str, int] = {}
    for path in _iter_downloadable_log_files():
        ymd = _local_ymd_from_log_name(path.name)
        if not ymd:
            continue
        days[ymd] = days.get(ymd, 0) + 1
    years: dict[str, dict[str, dict[str, int]]] = {}
    for ymd, count in sorted(days.items(), reverse=True):
        year, month, day = ymd[:4], ymd[4:6], ymd[6:8]
        years.setdefault(year, {}).setdefault(month, {})[day] = count
    latest = next(iter(sorted(days.keys(), reverse=True)), "")
    return {
        "ok": True,
        "timezone": "Asia/Seoul",
        "years": years,
        "days": days,
        "latest": latest,
        "file_count": sum(days.values()),
    }


def _list_logging_files_for_ymd(ymd: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in _iter_downloadable_log_files():
        if _local_ymd_from_log_name(path.name) != ymd:
            continue
        meta = _parse_log_display(path.name)
        try:
            size = path.stat().st_size
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=_LOCAL_TZ).isoformat()
        except OSError:
            continue
        rows.append(
            {
                "filename": path.name,
                "label": meta["label"],
                "stamp": meta["stamp"],
                "part": meta["part"],
                "download_bytes": size,
                "compressed": path.name.endswith(".gz"),
                "mtime": mtime,
                "download_url": f"/api/logging/files/download/{path.name}",
                "local_date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}",
            }
        )
    rows.sort(key=lambda item: item.get("stamp", ""), reverse=True)
    return rows


def _build_day_zip(ymd: str) -> Path | None:
    paths = [
        LOG_DIR / row["filename"]
        for row in _list_logging_files_for_ymd(ymd)
        if (LOG_DIR / row["filename"]).is_file()
    ]
    if not paths:
        return None
    zip_name = f"sensor_logs_{ymd}.zip"
    zip_path = LOG_DIR / zip_name
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, arcname=path.name)
    return zip_path


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

        match = re.match(r"sensor_log_(\d{8}_\d{6}(?:_\d{6})?)", path.name)
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
    def __init__(self, *, started_epoch_ms: int, label: str = "") -> None:
        self.started_epoch_ms = started_epoch_ms
        self.started_at = datetime.now(timezone.utc)
        self.label = _sanitize_log_label(label)
        stamp = self.started_at.strftime("%Y%m%d_%H%M%S_%f")
        # 예: 20260811_084517_123456_테스트주행
        self.session_id = f"{stamp}_{self.label}" if self.label else stamp
        self.recording_id = ""
        self.sync_command_id = ""
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
        self.summary_count = 0
        self.numeric_stats: dict[str, dict[str, float]] = {}
        self.previous_stm_ms: float | None = None
        self.stm_duplicate_count = 0
        self.stm_backward_count = 0
        self.max_xy_g = 0.0
        self.max_total_g = 0.0
        self.first_pi_mono_est_ns: int | None = None
        self.last_pi_mono_est_ns: int | None = None

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
            "label": self.label,
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

    def _update_summary(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            self.summary_count += 1
            for field in REPORT_NUMERIC_FIELDS:
                value = _as_float(row.get(field))
                if value is None:
                    continue
                stats = self.numeric_stats.get(field)
                if stats is None:
                    self.numeric_stats[field] = {
                        "min": value,
                        "max": value,
                        "sum": value,
                        "count": 1.0,
                    }
                else:
                    stats["min"] = min(stats["min"], value)
                    stats["max"] = max(stats["max"], value)
                    stats["sum"] += value
                    stats["count"] += 1.0

            stm_ms = _as_float(row.get("stm_ms"))
            if stm_ms is not None and self.previous_stm_ms is not None:
                if stm_ms == self.previous_stm_ms:
                    self.stm_duplicate_count += 1
                elif stm_ms < self.previous_stm_ms:
                    self.stm_backward_count += 1
            if stm_ms is not None:
                self.previous_stm_ms = stm_ms

            pi_mono_est_ns = _as_int(row.get("pi_mono_est_ns"))
            if pi_mono_est_ns is not None:
                if self.first_pi_mono_est_ns is None:
                    self.first_pi_mono_est_ns = pi_mono_est_ns
                self.last_pi_mono_est_ns = pi_mono_est_ns

            x_g = _as_float(row.get("x_g")) or 0.0
            y_g = _as_float(row.get("y_g")) or 0.0
            z_g = _as_float(row.get("z_g")) or 0.0
            self.max_xy_g = max(self.max_xy_g, math.hypot(x_g, y_g))
            self.max_total_g = max(self.max_total_g, math.sqrt(x_g * x_g + y_g * y_g + z_g * z_g))

    def build_report(
        self,
        *,
        stopped_at: str,
        camera_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        stopped_dt = datetime.fromisoformat(stopped_at)
        duration_sec = max(0.001, (stopped_dt - self.started_at).total_seconds())
        fields: dict[str, dict[str, float]] = {}
        for field, stats in self.numeric_stats.items():
            count = max(1.0, stats["count"])
            fields[field] = {
                "min": round(stats["min"], 4),
                "max": round(stats["max"], 4),
                "mean": round(stats["sum"] / count, 4),
                "range": round(stats["max"] - stats["min"], 4),
            }

        camera = camera_result or {}
        return {
            "session_id": self.session_id,
            "started_at": self.started_at.isoformat(),
            "stopped_at": stopped_at,
            "duration_sec": round(duration_sec, 3),
            "rows": self.rows_written,
            "average_hz": round(self.rows_written / duration_sec, 1),
            "dropped_rows": self.dropped_rows,
            "stm_duplicate_count": self.stm_duplicate_count,
            "stm_backward_count": self.stm_backward_count,
            "first_pi_mono_est_ns": self.first_pi_mono_est_ns,
            "last_pi_mono_est_ns": self.last_pi_mono_est_ns,
            "max_xy_g": round(self.max_xy_g, 3),
            "max_total_g": round(self.max_total_g, 3),
            "fields": fields,
            "camera_ok": bool(camera.get("ok")),
            "camera_filename": str(camera.get("filename") or ""),
            "camera_bytes": int(camera.get("file_size") or camera.get("bytes_written") or 0),
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
                self._update_summary(chunk)
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

                self._flush_pending(force=self.stop_requested)
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


def _stm_timestamp_from_sample(sample: dict[str, Any]) -> float | None:
    """Prefer continuous STM time; fall back to raw stm_ms aliases."""
    for key in ("stm_extended_ms", "stm_ms", "t", "ts", "timestamp_ms"):
        value = _as_float(sample.get(key))
        if value is not None:
            return float(value)
    return None


def extract_stm_timestamps_ms(body: Any) -> list[float]:
    """Extract per-sample STM timestamps (ms) for Hz calculation."""
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return []
    if not isinstance(body, dict):
        return []

    samples = body.get("samples")
    if isinstance(samples, list) and samples:
        stamps: list[float] = []
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            stamp = _stm_timestamp_from_sample(sample)
            if stamp is not None:
                stamps.append(stamp)
        return stamps

    stamp = _stm_timestamp_from_sample(body)
    return [stamp] if stamp is not None else []


def _append_sample_timestamps(
    target: deque[float],
    *,
    stm_timestamps_ms: list[float] | None = None,
    sample_count: int = 1,
) -> int:
    """Append STM ms stamps when available; otherwise use local monotonic ms."""
    if stm_timestamps_ms:
        for stamp in stm_timestamps_ms:
            target.append(float(stamp))
        return len(stm_timestamps_ms)

    safe_count = max(1, sample_count)
    now_ms = monotonic() * 1000.0
    for _ in range(safe_count):
        target.append(now_ms)
    return safe_count


def _hz_from_timestamps(
    timestamps: deque[float],
    window_sec: float = 1.0,
    *,
    last_recv_mono: float = 0.0,
) -> float:
    """STM 간격으로 Hz를 계산하되, 최근 수신이 없으면 0."""
    if len(timestamps) < 2:
        return 0.0
    # 데이터가 끊기면 STM 시계가 멈추므로, 벽시계 수신 시각으로 살아있음을 판정한다.
    if last_recv_mono <= 0.0 or (monotonic() - last_recv_mono) > max(1.0, float(window_sec)):
        return 0.0

    window_ms = max(1.0, float(window_sec) * 1000.0)
    newest = timestamps[-1]
    oldest = newest
    count = 1
    for stamp in reversed(list(timestamps)[:-1]):
        if stamp > oldest:
            # Walking backward should be non-increasing; stop on wrap/reboot/reorder.
            break
        if newest - stamp > window_ms:
            break
        oldest = stamp
        count += 1

    span_ms = newest - oldest
    if span_ms <= 0:
        return 0.0
    return (count - 1) * 1000.0 / span_ms


def record_traffic(
    sample_count: int = 1,
    *,
    stm_timestamps_ms: list[float] | None = None,
) -> None:
    global total_packets, total_samples, last_traffic_mono
    total_packets += 1
    last_traffic_mono = monotonic()
    added = _append_sample_timestamps(
        sample_times,
        stm_timestamps_ms=stm_timestamps_ms,
        sample_count=sample_count,
    )
    total_samples += added


def hz_last(window_sec: float = 1.0) -> float:
    return _hz_from_timestamps(
        sample_times,
        window_sec,
        last_recv_mono=last_traffic_mono,
    )


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


def record_platform_traffic(
    payload: dict[str, Any],
    sample_count: int = 1,
    *,
    stm_timestamps_ms: list[float] | None = None,
) -> None:
    platform_key = _platform_key_from_payload(payload)
    if platform_key not in sample_times_by_platform:
        return

    last_traffic_mono_by_platform[platform_key] = monotonic()
    _append_sample_timestamps(
        sample_times_by_platform[platform_key],
        stm_timestamps_ms=stm_timestamps_ms,
        sample_count=sample_count,
    )


def hz_last_for_platform(platform_key: str, window_sec: float = 1.0) -> float:
    timestamps = sample_times_by_platform.get(platform_key)
    if not timestamps:
        return 0.0
    return _hz_from_timestamps(
        timestamps,
        window_sec,
        last_recv_mono=last_traffic_mono_by_platform.get(platform_key, 0.0),
    )


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
        latest_wide_sample_by_device,
        latest_wide_fields_by_device,
        latest_stm_state_by_device,
        offset_sample_history_by_device,
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


def _extract_stm_ms(sample: dict[str, Any]) -> int | str:
    """STM 샘플 시간(ms). stm_ms / t / ts / timestamp_ms."""
    for key in ("stm_ms", "t", "ts", "timestamp_ms"):
        sample_ms = _as_int(sample.get(key))
        if sample_ms is not None:
            return int(sample_ms)
    return ""


def _extract_stm_time_ns(sample: dict[str, Any]) -> int | str:
    """하위 호환: 예전 ns 컬럼 경로용."""
    ms = _extract_stm_ms(sample)
    if ms == "" or ms is None:
        return ""
    return int(ms) * 1_000_000


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

    device_key = str(parsed.get("device") or parsed.get("d") or _device_key_from_payload(payload))
    wide_sample, present_fields = _extract_wide_live_sample(parsed)
    if wide_sample:
        latest_wide_sample_by_device[device_key] = wide_sample
        latest_wide_fields_by_device[device_key] = present_fields
        stm_value = _as_float(wide_sample.get("stm_ms"))
        if stm_value is not None:
            now_mono = monotonic()
            stm_state = latest_stm_state_by_device.get(device_key)
            if stm_state is None:
                latest_stm_state_by_device[device_key] = {
                    "value": stm_value,
                    "changed_at": now_mono,
                }
            elif stm_value != stm_state.get("value"):
                stm_state["value"] = stm_value
                stm_state["changed_at"] = now_mono

    snapshot = extract_offset_candidates(parsed)
    if not snapshot:
        return

    latest_sensor_snapshot_by_device[device_key] = snapshot
    history = offset_sample_history_by_device.setdefault(
        device_key,
        deque(maxlen=max(300, SAMPLE_TIMES_MAXLEN)),
    )
    now_mono = monotonic()
    history.append((now_mono, dict(snapshot)))
    while history and history[0][0] < now_mono - 10.0:
        history.popleft()
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
    """휠 RPM 계산은 사용하지 않음 (Pi에서 FR/FL/RR/RL만 송신)."""
    return payload


def _swap_dict_keys(data: dict[str, Any], key_a: str, key_b: str) -> None:
    if key_a not in data and key_b not in data:
        return
    data[key_a], data[key_b] = data.get(key_b), data.get(key_a)


_FL_RR_OFFSET_PATH_PAIRS = (
    ("car.linear.front_left", "car.linear.rear_right"),
)
_offsets_fl_rr_label_migrated = False


def swap_fl_rr_offset_paths(offsets: dict[str, float]) -> dict[str, float]:
    """오프셋 path 키의 FL↔RR 을 맞바꾼다 (값 스왑과 동일 좌표계)."""
    out = dict(offsets)
    for path_a, path_b in _FL_RR_OFFSET_PATH_PAIRS:
        has_a = path_a in out
        has_b = path_b in out
        if not has_a and not has_b:
            continue
        val_a = out.pop(path_a, None)
        val_b = out.pop(path_b, None)
        if val_b is not None:
            out[path_a] = val_b
        if val_a is not None:
            out[path_b] = val_a
    return out


def migrate_device_offsets_to_swapped_labels() -> None:
    """스왑 전에 잡힌 기존 오프셋을 스왑 후 라벨 공간으로 한 번 옮긴다."""
    global device_offsets, _offsets_fl_rr_label_migrated
    if _offsets_fl_rr_label_migrated:
        return
    for device_key, offs in list(device_offsets.items()):
        if isinstance(offs, dict) and offs:
            device_offsets[device_key] = swap_fl_rr_offset_paths(offs)
    _offsets_fl_rr_label_migrated = True


def _swap_fl_rr_in_sample(sample: dict[str, Any]) -> None:
    """배선/채널이 뒤바뀐 FL↔RR 만 교정 (FR/RL 은 그대로)."""
    _swap_dict_keys(sample, "FL", "RR")
    _swap_dict_keys(sample, "fl", "rr")
    _swap_dict_keys(sample, "linear_fl", "linear_rr")

    linear = sample.get("linear")
    if isinstance(linear, dict):
        _swap_dict_keys(linear, "fl", "rr")
        _swap_dict_keys(linear, "FL", "RR")
        _swap_dict_keys(linear, "front_left", "rear_right")

    car = sample.get("car")
    if isinstance(car, dict):
        car_linear = car.get("linear")
        if isinstance(car_linear, dict):
            _swap_dict_keys(car_linear, "fl", "rr")
            _swap_dict_keys(car_linear, "FL", "RR")
            _swap_dict_keys(car_linear, "front_left", "rear_right")


def swap_fl_rr_in_parsed(parsed: dict[str, Any]) -> bool:
    changed = False
    samples = parsed.get("samples")
    if isinstance(samples, list):
        for sample in samples:
            if isinstance(sample, dict):
                _swap_fl_rr_in_sample(sample)
                changed = True
    _swap_fl_rr_in_sample(parsed)
    changed = True
    return changed


def apply_linear_channel_swap_to_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """수신 직후 FL↔RR 교정. CSV·실시간 포워드에 동일하게 반영."""
    message = payload.get("message", "")
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        return payload
    if not isinstance(parsed, dict):
        return payload
    swap_fl_rr_in_parsed(parsed)
    return {**payload, "message": json.dumps(parsed, ensure_ascii=False)}


def prepare_sensor_payload(
    payload: dict[str, Any],
    *,
    update_snapshot: bool = True,
) -> dict[str, Any]:
    """FL↔RR 교정 → (오프셋 스냅샷) → 오프셋 적용 → 휠 메트릭.

    오프셋 보정은 스왑 이후 라벨(실제 코너) 기준으로 잡아야 하므로
    스냅샷은 반드시 채널 스왑 이후에 갱신한다.
    """
    migrate_device_offsets_to_swapped_labels()
    payload = apply_linear_channel_swap_to_payload(payload)
    if update_snapshot:
        update_latest_sensor_snapshot(payload)
    payload = apply_offsets_to_payload(payload)
    payload = apply_wheel_speed_to_payload(payload)
    return payload


def _forward_body(payload: dict[str, Any]) -> bytes:
    """로깅으로 들어온 배치에서 최신 샘플만 실시간에 넘긴다 (≈20Hz UI).

    CSV 로깅은 전체 samples(200Hz)를 유지하고, 모니터는 배치 마지막 값만 본다.
    """
    message = payload.get("message", "")
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        body = dict(parsed)
        samples = body.get("samples")
        if isinstance(samples, list) and samples:
            last = samples[-1]
            body["samples"] = [last] if isinstance(last, dict) else samples[-1:]
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


def _camera_api_json(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    if not LINK_CAMERA_RECORDING or not CAMERA_INTERNAL_URL:
        return {"ok": False, "linked": False, "message": "camera linking disabled"}
    url = f"{CAMERA_INTERNAL_URL}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    timeout = CAMERA_API_TIMEOUT_SEC if timeout_sec is None else max(1.0, float(timeout_sec))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
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
        cam_dir = Path(__file__).resolve().parent.parent / "udp_realtime" / "camera_recordings"
        cam_dir.mkdir(parents=True, exist_ok=True)
        (cam_dir / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with suppress(OSError, json.JSONDecodeError, ValueError):
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    return {}


def _nearest_sensor_row(
    session_id: str,
    target_pi_mono_ns: int,
) -> tuple[dict[str, Any] | None, int | None]:
    candidates = sorted(LOG_DIR.glob(f"sensor_log_{session_id}*.csv"))
    candidates.extend(sorted(LOG_DIR.glob(f"sensor_log_{session_id}*.csv.gz")))
    nearest: dict[str, Any] | None = None
    nearest_delta: int | None = None
    for path in candidates:
        opener = gzip.open if path.name.endswith(".gz") else open
        with suppress(OSError, UnicodeError, csv.Error):
            with opener(path, "rt", encoding="utf-8-sig", newline="") as fp:
                for row in csv.DictReader(fp):
                    pi_ns = _as_int(row.get("pi_mono_est_ns"))
                    if pi_ns is None:
                        continue
                    delta = pi_ns - target_pi_mono_ns
                    if nearest_delta is None or abs(delta) < abs(nearest_delta):
                        nearest = dict(row)
                        nearest_delta = delta
    return nearest, nearest_delta


def _device_token_error(request: Request) -> Response | None:
    if not CAMERA_SYNC_SERVER_TOKEN:
        return Response(
            content=json.dumps(
                {"ok": False, "message": "CAMERA_SYNC_SERVER_TOKEN이 설정되지 않았습니다."},
                ensure_ascii=False,
            ),
            media_type="application/json",
            status_code=503,
        )
    authorization = request.headers.get("authorization", "")
    scheme, _, supplied = authorization.partition(" ")
    valid = (
        scheme.lower() == "bearer"
        and bool(supplied)
        and secrets.compare_digest(supplied, CAMERA_SYNC_SERVER_TOKEN)
    )
    if valid:
        return None
    return Response(
        content=json.dumps({"ok": False, "message": "invalid device token"}),
        media_type="application/json",
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _validate_sync_report(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("JSON object가 필요합니다.")
    string_fields = (
        "device",
        "command_id",
        "recording_id",
        "session_id",
        "pi_boot_id",
        "camera_clock_source",
    )
    integer_fields = (
        "first_frame_pi_mono_ns",
        "first_frame_pts_ns",
        "stream_first_sensor_mono_ns",
        "rtp_clock_rate",
        "reported_pi_mono_ns",
    )
    normalized = dict(body)
    for field in string_fields:
        value = str(body.get(field) or "").strip()
        if not value:
            raise ValueError(f"{field}가 필요합니다.")
        normalized[field] = value
    for field in integer_fields:
        value = body.get(field)
        if isinstance(value, bool):
            raise ValueError(f"{field}는 정수여야 합니다.")
        try:
            normalized[field] = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field}는 정수여야 합니다.") from exc
    if normalized["rtp_clock_rate"] != 90000:
        raise ValueError("영상 rtp_clock_rate는 90000이어야 합니다.")
    rtp_timestamp = body.get("rtp_timestamp")
    if rtp_timestamp is not None:
        if isinstance(rtp_timestamp, bool):
            raise ValueError("rtp_timestamp는 정수 또는 null이어야 합니다.")
        try:
            normalized["rtp_timestamp"] = int(rtp_timestamp) & 0xFFFF_FFFF
        except (TypeError, ValueError) as exc:
            raise ValueError("rtp_timestamp는 정수 또는 null이어야 합니다.") from exc
    else:
        normalized["rtp_timestamp"] = None
    return normalized


@app.get("/api/sync/commands/next")
def camera_sync_next(
    request: Request,
    device: str,
    pi_boot_id: str,
) -> Response:
    auth_error = _device_token_error(request)
    if auth_error is not None:
        return auth_error
    device = device.strip()
    pi_boot_id = pi_boot_id.strip()
    if not device or not pi_boot_id:
        return Response(
            content=json.dumps(
                {"ok": False, "message": "device와 pi_boot_id가 필요합니다."},
                ensure_ascii=False,
            ),
            media_type="application/json",
            status_code=422,
        )
    command = camera_sync_store.next_command(device, pi_boot_id)
    if command is None:
        return Response(status_code=204)
    return Response(
        content=json.dumps(
            {
                "command_id": command["command_id"],
                "recording_id": command["recording_id"],
                "session_id": command["session_id"],
            },
            ensure_ascii=False,
        ),
        media_type="application/json",
        status_code=200,
    )


@app.post("/api/sync/report")
async def camera_sync_report(request: Request) -> Response:
    auth_error = _device_token_error(request)
    if auth_error is not None:
        return auth_error
    try:
        report = _validate_sync_report(await request.json())
    except (json.JSONDecodeError, ValueError) as exc:
        return Response(
            content=json.dumps(
                {"ok": False, "duplicate": False, "message": str(exc)},
                ensure_ascii=False,
            ),
            media_type="application/json",
            status_code=422,
        )

    try:
        saved = camera_sync_store.save_report(report)
        effective_report = (
            dict(saved["payload"])
            if saved.get("duplicate") and isinstance(saved.get("payload"), dict)
            else report
        )
        sidecar_path = _write_camera_sync_sidecar(
            effective_report["session_id"],
            str(saved["video_filename"]),
            {
                **effective_report,
                "sync_status": "ready",
                "reported_via": "pi_outbound_report",
            },
        )
        _write_session_sync(
            effective_report["session_id"],
            {
                "recording_id": effective_report["recording_id"],
                "sync_command_id": effective_report["command_id"],
                "pi_sync_status": "ready",
                "pi_sync": effective_report,
                "video_sync_sidecar": str(sidecar_path),
                "offset_sec_hint": None,
                "sync_rule": "pi_boottime_pending_recording_first_rtp",
            },
        )
        camera_sync_store.complete_command(effective_report["command_id"])
    except SyncReportError as exc:
        return Response(
            content=json.dumps(
                {"ok": False, "duplicate": False, "message": str(exc)},
                ensure_ascii=False,
            ),
            media_type="application/json",
            status_code=409,
        )
    except OSError as exc:
        return Response(
            content=json.dumps(
                {
                    "ok": False,
                    "duplicate": bool(locals().get("saved", {}).get("duplicate")),
                    "message": f"sync sidecar 저장 실패: {exc}",
                },
                ensure_ascii=False,
            ),
            media_type="application/json",
            status_code=500,
        )
    return Response(
        content=json.dumps(
            {
                "ok": True,
                "duplicate": bool(saved["duplicate"]),
                "command_id": effective_report["command_id"],
                "recording_id": effective_report["recording_id"],
                "session_id": effective_report["session_id"],
                "sidecar": str(sidecar_path),
            },
            ensure_ascii=False,
        ),
        media_type="application/json",
        status_code=200,
    )


@app.get("/api/sync/timeline")
def sync_timeline(session_id: str, video_pts_ns: int) -> Any:
    safe_session_id = Path(session_id).name
    if safe_session_id != session_id or not session_id:
        return Response(
            content=json.dumps({"ok": False, "message": "잘못된 session_id"}),
            media_type="application/json",
            status_code=400,
        )
    session_sync = _read_json_object(_session_sync_path(session_id))
    video_filename = str(
        session_sync.get("camera_filename")
        or _camera_recording_filename(session_id)
    )
    sidecar = _read_json_object(_camera_sync_sidecar_path(video_filename))
    rtcp = sidecar.get("rtcp_mapping")
    if not isinstance(rtcp, dict):
        rtcp = {}
    recording_first_pi_ns = _as_int(
        rtcp.get("recording_first_rtp_pi_mono_ns")
    )
    if recording_first_pi_ns is not None:
        frame_pi_ns = recording_first_pi_ns + int(video_pts_ns)
        sensor_row, sensor_delta_ns = _nearest_sensor_row(
            session_id,
            frame_pi_ns,
        )
        return {
            "ok": True,
            "mapping_ready": True,
            "session_id": session_id,
            "video_pts_ns": video_pts_ns,
            "video_frame_pi_mono_ns": frame_pi_ns,
            "rtp_clock_rate": _as_int(rtcp.get("rtp_clock_rate")) or 90000,
            "sensor_delta_ns": sensor_delta_ns,
            "sensor": sensor_row,
            "mapping_source": rtcp.get("source"),
        }

    required = {
        "pi_rtp_anchor": sidecar.get("rtp_timestamp"),
        "pi_mono_anchor_ns": sidecar.get("first_frame_pi_mono_ns"),
        "recording_first_rtp": rtcp.get("recording_first_rtp_timestamp"),
    }
    missing = [key for key, value in required.items() if value is None]
    if missing:
        return Response(
            content=json.dumps(
                {
                    "ok": False,
                    "mapping_ready": False,
                    "session_id": session_id,
                    "missing": missing,
                    "reason": rtcp.get("reason")
                    or "RTP↔Pi CLOCK_BOOTTIME anchor가 없습니다.",
                },
                ensure_ascii=False,
            ),
            media_type="application/json",
            status_code=409,
        )

    rate = _as_int(sidecar.get("rtp_clock_rate")) or _as_int(
        rtcp.get("rtp_clock_rate")
    ) or 90000
    frame_pi_ns = video_pts_to_pi_mono_ns(
        video_pts_ns=video_pts_ns,
        pi_rtp_anchor=int(required["pi_rtp_anchor"]),
        pi_mono_anchor_ns=int(required["pi_mono_anchor_ns"]),
        recording_first_rtp=int(required["recording_first_rtp"]),
        rtp_clock_rate=rate,
    )
    sensor_row, sensor_delta_ns = _nearest_sensor_row(session_id, frame_pi_ns)
    return {
        "ok": True,
        "mapping_ready": True,
        "session_id": session_id,
        "video_pts_ns": video_pts_ns,
        "video_frame_pi_mono_ns": frame_pi_ns,
        "rtp_clock_rate": rate,
        "sensor_delta_ns": sensor_delta_ns,
        "sensor": sensor_row,
    }


def _camera_sync_sidecar_path(video_filename: str) -> Path:
    safe_name = Path(video_filename).name
    camera_dir = (
        Path(__file__).resolve().parent.parent
        / "udp_realtime"
        / "camera_recordings"
    )
    camera_dir.mkdir(parents=True, exist_ok=True)
    return camera_dir / f"{safe_name}.sync.json"


def _write_camera_sync_sidecar(
    session_id: str,
    video_filename: str,
    payload: dict[str, Any],
) -> Path:
    path = _camera_sync_sidecar_path(
        video_filename or _camera_recording_filename(session_id)
    )
    clean_payload = {
        key: value
        for key, value in payload.items()
        if not key.startswith("_")
    }
    sidecar = {
        **clean_payload,
        "session_id": session_id,
        "video_filename": video_filename or _camera_recording_filename(session_id),
        "server_saved_at": datetime.now(timezone.utc).isoformat(),
        "rtcp_mapping": {
            "status": "unavailable",
            "ssrc": None,
            "sender_report_rtp_timestamp": None,
            "rtp_clock_rate": clean_payload.get("rtp_clock_rate") or 90000,
            "ntp_timestamp": None,
            "recording_first_rtp_timestamp": None,
            "reason": (
                "현재 MediaMTX 설정/API와 FFmpeg subprocess remux는 RTCP Sender "
                "Report 및 녹화 첫 RTP packet timestamp를 애플리케이션에 노출하지 않음"
            ),
        },
        "mapping_ready": False,
        "mapping_note": (
            "Pi의 first_frame_pi_mono_ns만으로는 이미 진행 중인 RTSP 스트림에서 "
            "FFmpeg가 기록한 MP4 PTS 0의 RTP timestamp를 알 수 없어 정확한 변환 불가"
        ),
    }
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        temporary.write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        with suppress(OSError):
            temporary.unlink()
    return path


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        with suppress(OSError):
            temporary.unlink()


def _finalize_rtp_sync_sidecar(
    session_id: str,
    video_filename: str,
    rtp_sync: dict[str, Any],
) -> dict[str, Any]:
    path = _camera_sync_sidecar_path(
        video_filename or _camera_recording_filename(session_id)
    )
    sidecar = _read_json_object(path)
    recorder = rtp_sync.get("recorder")
    rtcp_sr = rtp_sync.get("rtcp_sr")
    recorder = recorder if isinstance(recorder, dict) else {}
    rtcp_sr = rtcp_sr if isinstance(rtcp_sr, dict) else {}
    recorder_ssrc = _as_int(recorder.get("ssrc"))
    sr_ssrc = _as_int(rtcp_sr.get("ssrc"))
    first_rtp = _as_int(recorder.get("first_rtp_timestamp"))
    sr_rtp = _as_int(rtcp_sr.get("rtp_timestamp"))
    sr_ntp_ns = _as_int(rtcp_sr.get("ntp_unix_ns"))
    rate = _as_int(recorder.get("rtp_clock_rate")) or 90000
    ssrc_match = (
        recorder_ssrc is not None
        and sr_ssrc is not None
        and recorder_ssrc == sr_ssrc
    )
    mapping: dict[str, Any] = {
        "status": "unavailable",
        "ssrc": recorder_ssrc,
        "sender_report_ssrc": sr_ssrc,
        "sender_report_rtp_timestamp": sr_rtp,
        "rtp_clock_rate": rate,
        "ntp_timestamp_ns": sr_ntp_ns,
        "ntp_seconds": rtcp_sr.get("ntp_seconds"),
        "ntp_fraction": rtcp_sr.get("ntp_fraction"),
        "recording_first_rtp_timestamp": first_rtp,
        "recording_first_sequence": recorder.get("first_sequence"),
        "ssrc_match": ssrc_match,
        "source": rtp_sync.get("mapping_source"),
    }
    mapping_ready = False
    first_rtp_ntp_ns: int | None = None
    first_rtp_pi_mono_ns: int | None = None
    if not ssrc_match:
        mapping["reason"] = "녹화 연결과 RTCP probe의 SSRC가 일치하지 않음"
    elif first_rtp is None or sr_rtp is None or sr_ntp_ns is None:
        mapping["reason"] = "RTP-Info 또는 RTCP Sender Report 필드가 없음"
    else:
        first_rtp_ntp_ns = rtp_to_ntp_ns(
            rtp_timestamp=first_rtp,
            sender_report_rtp_timestamp=sr_rtp,
            sender_report_ntp_ns=sr_ntp_ns,
            rtp_clock_rate=rate,
        )
        mapping["recording_first_rtp_ntp_ns"] = first_rtp_ntp_ns
        reported_pi_realtime_ns = _as_int(
            sidecar.get("reported_pi_realtime_ns")
        )
        reported_pi_mono_ns = _as_int(sidecar.get("reported_pi_mono_ns"))
        if (
            reported_pi_realtime_ns is None
            or reported_pi_mono_ns is None
        ):
            mapping["status"] = "rtp_ntp_ready"
            mapping["reason"] = (
                "Pi report에 reported_pi_realtime_ns가 없어 "
                "NTP↔Pi CLOCK_BOOTTIME 변환 불가"
            )
        else:
            first_rtp_pi_mono_ns = (
                first_rtp_ntp_ns
                + reported_pi_mono_ns
                - reported_pi_realtime_ns
            )
            mapping.update(
                {
                    "status": "ready",
                    "recording_first_rtp_pi_mono_ns": first_rtp_pi_mono_ns,
                    "pi_realtime_minus_boottime_ns": (
                        reported_pi_realtime_ns - reported_pi_mono_ns
                    ),
                }
            )
            mapping_ready = True

    sidecar.update(
        {
            "rtcp_mapping": mapping,
            "mapping_ready": mapping_ready,
            "recording_first_frame_pi_mono_ns": first_rtp_pi_mono_ns,
            "mapping_note": (
                "video_frame_pi_mono_ns = recording_first_rtp_pi_mono_ns "
                "+ video_pts_ns"
                if mapping_ready
                else mapping.get("reason", "")
            ),
            "server_rtp_finalized_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    _atomic_write_json(path, sidecar)
    return {
        "path": str(path),
        "mapping_ready": mapping_ready,
        "rtcp_mapping": mapping,
    }


def _start_linked_camera_recording(
    session_id: str,
    recording_id: str,
) -> dict[str, Any]:
    """로깅 시작 시 RTSP 녹화 시도. 실패해도 로깅은 계속한다."""
    try:
        body = {
            "session_id": session_id,
            "recording_id": recording_id,
            "filename": _camera_recording_filename(session_id),
            "capture": "camera",
            "max_frames": 0,
        }
        status = _camera_api_json("GET", "/api/recording/status")
        if status.get("active") or status.get("armed"):
            _camera_api_json(
                "POST",
                "/api/recording/stop",
                timeout_sec=CAMERA_STOP_TIMEOUT_SEC,
            )
            _camera_api_json("POST", "/api/recording/disarm")

        result = _camera_api_json("POST", "/api/recording/start", body)
        if result.get("ok") or result.get("already_active"):
            return {
                **result,
                "linked": True,
                "record_mode": str(result.get("record_mode") or "rtsp"),
                "capture": "camera",
            }

        armed = _camera_api_json("POST", "/api/recording/arm", body)
        return {
            **armed,
            "linked": True,
            "armed": bool(armed.get("armed") or armed.get("ok")),
            "record_mode": str(armed.get("record_mode") or "rtsp"),
            "capture": "camera",
            "start_error": result.get("message", ""),
            "logging_ok_without_camera": True,
        }
    except Exception as exc:
        return {
            "ok": False,
            "linked": True,
            "record_mode": "rtsp",
            "capture": "camera",
            "message": f"카메라 연동 실패(로깅은 계속): {exc}",
            "logging_ok_without_camera": True,
        }


def _stop_linked_camera_recording() -> dict[str, Any]:
    """로깅 중지와 동시에 녹화를 반드시 내린다. 실패 시 재시도."""
    if not LINK_CAMERA_RECORDING or not CAMERA_INTERNAL_URL:
        return {"ok": True, "linked": False, "message": "camera linking disabled", "active": False}

    last_stop: dict[str, Any] = {}
    last_status: dict[str, Any] = {}
    disarm_result: dict[str, Any] = {}
    try:
        for attempt in range(1, CAMERA_STOP_RETRIES + 1):
            status_before = _camera_api_json("GET", "/api/recording/status")
            last_status = status_before
            active = bool(status_before.get("active"))
            armed = bool(status_before.get("armed"))
            if not active and not armed:
                return {
                    "ok": True,
                    "linked": True,
                    "active": False,
                    "armed": False,
                    "already_stopped": True,
                    "attempts": attempt,
                    "filename": str(status_before.get("filename") or ""),
                    "bytes_written": status_before.get("bytes_written", 0),
                    "file_size": status_before.get("file_size", 0),
                    "segments": list(status_before.get("segments") or []),
                    "rtp_sync": dict(status_before.get("rtp_sync") or {}),
                    "message": "camera already stopped",
                    "camera_stopped_at": datetime.now(timezone.utc).isoformat(),
                }

            last_stop = _camera_api_json(
                "POST",
                "/api/recording/stop",
                timeout_sec=CAMERA_STOP_TIMEOUT_SEC,
            )
            disarm_result = _camera_api_json("POST", "/api/recording/disarm")
            status_after = _camera_api_json("GET", "/api/recording/status")
            last_status = status_after
            still_active = bool(status_after.get("active") or status_after.get("armed"))
            recording = last_stop.get("recording") if isinstance(last_stop.get("recording"), dict) else {}
            if not still_active:
                return {
                    "ok": True,
                    "linked": True,
                    "record_mode": str(
                        recording.get("record_mode")
                        or last_stop.get("record_mode")
                        or status_before.get("record_mode")
                        or "rtsp"
                    ),
                    "active": False,
                    "armed": False,
                    "attempts": attempt,
                    "filename": str(
                        recording.get("filename")
                        or last_stop.get("filename")
                        or status_before.get("filename")
                        or ""
                    ),
                    "bytes_written": recording.get("bytes_written", status_before.get("bytes_written", 0)),
                    "file_size": int(
                        recording.get("file_size")
                        or last_stop.get("file_size")
                        or status_after.get("file_size")
                        or 0
                    ),
                    "segments": list(
                        last_stop.get("segments")
                        or recording.get("segments")
                        or status_before.get("segments")
                        or []
                    ),
                    "rtp_sync": dict(
                        recording.get("rtp_sync")
                        or last_stop.get("rtp_sync")
                        or status_after.get("rtp_sync")
                        or status_before.get("rtp_sync")
                        or {}
                    ),
                    "stop": last_stop,
                    "disarm": disarm_result,
                    "message": str(
                        last_stop.get("message")
                        or disarm_result.get("message")
                        or "camera recording stopped"
                    ),
                    "camera_stopped_at": datetime.now(timezone.utc).isoformat(),
                }
            print(
                f"[camera-link] stop retry {attempt}/{CAMERA_STOP_RETRIES} "
                f"still_active={still_active} msg={last_stop.get('message')}"
            )

        return {
            "ok": False,
            "linked": True,
            "active": bool(last_status.get("active")),
            "armed": bool(last_status.get("armed")),
            "attempts": CAMERA_STOP_RETRIES,
            "filename": str(last_status.get("filename") or ""),
            "stop": last_stop,
            "disarm": disarm_result,
            "message": str(
                last_stop.get("message")
                or "카메라 녹화가 계속 활성입니다. 카메라 서버에서 수동 중지하세요."
            ),
            "camera_stopped_at": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "linked": True,
            "active": True,
            "message": f"카메라 중지 실패: {exc}",
            "camera_stopped_at": "",
        }


def _linked_camera_recording_status() -> dict[str, Any]:
    if not LINK_CAMERA_RECORDING:
        return {"linked": False}
    status = _camera_api_json("GET", "/api/recording/status")
    return {**status, "linked": True, "record_mode": str(status.get("record_mode") or status.get("record_mode_config") or "rtsp")}


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
    # 실시간 모니터는 최신 페이로드만 유지한다.
    # 큐에 옛 배치가 쌓이면 STM 전원이 끊긴 뒤에도 과거 값이 계속 재생된다.
    dropped = 0
    while True:
        try:
            queue.get_nowait()
            dropped += 1
        except asyncio.QueueEmpty:
            break
    if dropped:
        forward_dropped_count += dropped
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
    # time
    "stm_ms": "stm_ms",
    "t": "stm_ms",
    "ts": "stm_ms",
    "timestamp_ms": "stm_ms",
    # suspension / travel (Pi: FR FL RR RL)
    "fr": "FR",
    "fl": "FL",
    "rr": "RR",
    "rl": "RL",
    "linear_fr": "FR",
    "linear_fl": "FL",
    "linear_rr": "RR",
    "linear_rl": "RL",
    "front_right": "FR",
    "front_left": "FL",
    "rear_right": "RR",
    "rear_left": "RL",
    # accel g
    "x_g": "x_g",
    "y_g": "y_g",
    "z_g": "z_g",
    "accelx": "x_g",
    "accely": "y_g",
    "accelz": "z_g",
    "ax": "x_g",
    "ay": "y_g",
    "az": "z_g",
    "x": "x_g",
    "y": "y_g",
    "z": "z_g",
    # ecu
    "ecu_temp": "ecu_temp",
    "ecu": "ecu_temp",
    "tp": "ecu_temp",
    # steering
    "steering_s": "steering_speed",
    "steering_speed": "steering_speed",
    "steering_a": "steering_angle",
    "steering_angle": "steering_angle",
    # inverter
    "inv_temp_igt": "inv_temp_igt",
    "inv_temp_igbt": "inv_temp_igt",
    "inv_temp_motor": "inv_temp_motor",
    "inv_moter_speed": "inv_motor_speed",
    "inv_motor_speed": "inv_motor_speed",
    "inv_dc_currnet": "inv_dc_current",
    "inv_dc_current": "inv_dc_current",
    "inv_voltage": "inv_voltage",
    # Cascadia torque / Id Iq
    "inv_shudder_torque": "INV_shudder_torque",
    "inv_torque_feedback": "INV_torque_feedback",
    "inv_torque_commanded": "INV_torque_commanded",
    "inv_id_feedback": "INV_id_feedback",
    "inv_iq_feedback": "INV_iq_feedback",
    "inv_id_command": "INV_id_command",
    "inv_iq_command": "INV_iq_command",
    # CAN link status
    "can1": "can1",
    "can2": "can2",
    # motor status
    "motor_precharge": "motor_precharge",
    "precharge": "motor_precharge",
    "motor_main_contactor": "motor_main_contactor",
    "main_contactor": "motor_main_contactor",
    "motor_inverter_mode": "motor_inverter_mode",
    "inverter_mode": "motor_inverter_mode",
    "inv_mode": "motor_inverter_mode",
    # bms
    "bms_charge": "bms_charge",
    "bms_capacity": "bms_capacity",
    "bms_voltage": "bms_voltage",
    "bms_current": "bms_current",
    "bms_ccl": "bms_ccl",
    "bms_dcl": "bms_dcl",
    "bms_temp_maxvalue": "bms_temp_maxvalue",
}


def _empty_wide_row(payload: dict[str, Any]) -> dict[str, Any]:
    dt = _payload_time_to_datetime(payload)
    return {
        "timestamp": _format_recording_timestamp(dt),
        "stm_ms": "",
        "schema_version": "",
        "pi_boot_id": "",
        "sender_instance_id": "",
        "batch_seq": "",
        "uart_rx_mono_ns": "",
        "pi_clock_source": "",
        "stm_epoch": "",
        "stm_wrap_count": "",
        "stm_extended_ms": "",
        "pi_mono_fixed_5ms_ns": "",
        "pi_mono_est_ns": "",
        "FR": 0,
        "FL": 0,
        "RR": 0,
        "RL": 0,
        "x_g": 0,
        "y_g": 0,
        "z_g": 0,
        "ecu_temp": 0,
        "steering_angle": 0,
        "steering_speed": 0,
        "inv_temp_igt": 0,
        "inv_temp_motor": 0,
        "inv_motor_speed": 0,
        "vehicle_speed_kmh": 0,
        "inv_dc_current": 0,
        "inv_voltage": 0,
        "motor_precharge": 0,
        "motor_main_contactor": 0,
        "motor_inverter_mode": 0,
        "INV_shudder_torque": 0,
        "INV_id_feedback": 0,
        "INV_iq_feedback": 0,
        "INV_torque_commanded": 0,
        "INV_torque_feedback": 0,
        "INV_id_command": 0,
        "INV_iq_command": 0,
        "bms_charge": 0,
        "bms_voltage": 0,
        "bms_current": 0,
        "bms_ccl": 0,
        "bms_dcl": 0,
        "bms_temp_maxvalue": 0,
        "bms_capacity": 0,
        "can1": 0,
        "can2": 0,
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
        accel2 = car.get("accel2")
        if isinstance(accel2, dict):
            _set_wide_value(row, "x_g", accel2.get("accel2_x"))
            _set_wide_value(row, "y_g", accel2.get("accel2_y"))
            _set_wide_value(row, "z_g", accel2.get("accel2_z"))

        steering = car.get("steering")
        if isinstance(steering, dict):
            _set_wide_value(row, "steering_speed", steering.get("speed"))
            _set_wide_value(row, "steering_angle", steering.get("angle"))

        linear = car.get("linear")
        if isinstance(linear, dict):
            _set_wide_value(row, "FL", linear.get("front_left"))
            _set_wide_value(row, "FR", linear.get("front_right"))
            _set_wide_value(row, "RL", linear.get("rear_left"))
            _set_wide_value(row, "RR", linear.get("rear_right"))

    linear = parsed.get("linear")
    if isinstance(linear, dict):
        for key, value in linear.items():
            if key == "fl":
                row["FL"] = value
            elif key == "fr":
                row["FR"] = value
            elif key == "rl":
                row["RL"] = value
            elif key == "rr":
                row["RR"] = value
            else:
                _set_wide_value(row, key, value)

    mcu_die_temp = _extract_mcu_die_temp(parsed)
    if mcu_die_temp is not None:
        row["ecu_temp"] = mcu_die_temp

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
            _set_wide_value(row, "inv_temp_motor", temperature.get("motor"))

        motor = inverter.get("motor")
        if isinstance(motor, dict):
            _set_wide_value(row, "inv_motor_speed", motor.get("speed"))
            _set_wide_value(row, "motor_precharge", motor.get("precharge"))
            _set_wide_value(row, "motor_main_contactor", motor.get("main_contactor") or motor.get("contactor"))
            _set_wide_value(row, "motor_inverter_mode", motor.get("mode") or motor.get("inverter_mode"))

        current = inverter.get("current")
        if isinstance(current, dict):
            _set_wide_value(row, "inv_dc_current", current.get("dc_bus"))

        voltage = inverter.get("voltage")
        if isinstance(voltage, dict):
            _set_wide_value(row, "inv_voltage", voltage.get("dc_bus"))

        torque = inverter.get("torque")
        if isinstance(torque, dict):
            _set_wide_value(row, "inv_shudder_torque", torque.get("shudder"))
            _set_wide_value(row, "inv_torque_feedback", torque.get("feedback"))
            _set_wide_value(row, "inv_torque_commanded", torque.get("commanded"))

        idq_feedback = inverter.get("feedback")
        if isinstance(idq_feedback, dict):
            _set_wide_value(row, "inv_id_feedback", idq_feedback.get("id"))
            _set_wide_value(row, "inv_iq_feedback", idq_feedback.get("iq"))

        idq_command = inverter.get("command")
        if isinstance(idq_command, dict):
            _set_wide_value(row, "inv_id_command", idq_command.get("id"))
            _set_wide_value(row, "inv_iq_command", idq_command.get("iq"))

        _set_wide_value(row, "motor_precharge", inverter.get("motor_precharge"))
        _set_wide_value(row, "motor_main_contactor", inverter.get("motor_main_contactor"))
        _set_wide_value(
            row,
            "motor_inverter_mode",
            inverter.get("motor_inverter_mode")
            or inverter.get("mode")
            or inverter.get("mode_name"),
        )

    can = parsed.get("can")
    if isinstance(can, dict):
        _set_wide_value(row, "can1", can.get("can1"))
        _set_wide_value(row, "can2", can.get("can2"))

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
            elif temp_max is not None:
                _set_wide_value(row, "bms_temp_maxvalue", temp_max)

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
        if key in {
            "device",
            "d",
            "s",
            "t",
            "ts",
            "samples",
            "linear",
            "accel",
            "accelerometer",
            "acceleration",
            "sensor_updates",
            "path",
            "value",
            "steering",
            "inverter",
            "bms",
            "can",
            "car",
            "ecu",
            "mcu",
        }:
            continue
        if isinstance(value, (dict, list)):
            continue
        _set_wide_value(row, key, value)


    # CSV에는 0/1 숫자로 기록 (0=Torque, 1=Speed)
    if "motor_inverter_mode" in row and row["motor_inverter_mode"] != "":
        try:
            raw = row["motor_inverter_mode"]
            if isinstance(raw, str):
                lowered = raw.strip().lower()
                if lowered in {"1", "speed", "spd"}:
                    row["motor_inverter_mode"] = 1
                elif lowered in {"0", "torque", "trq"}:
                    row["motor_inverter_mode"] = 0
                else:
                    row["motor_inverter_mode"] = 1 if float(lowered) else 0
            else:
                row["motor_inverter_mode"] = 1 if int(raw) else 0
        except Exception:
            row["motor_inverter_mode"] = 0
    for flag_key in ("motor_precharge", "motor_main_contactor", "can1", "can2"):
        if flag_key in row and row[flag_key] != "":
            try:
                raw = row[flag_key]
                if isinstance(raw, str):
                    row[flag_key] = 1 if raw.strip().lower() in {"1", "true", "yes", "on"} else 0
                else:
                    row[flag_key] = 1 if int(raw) else 0
            except Exception:
                row[flag_key] = 0

    speed = motor_rpm_to_speed_kmh(row.get("inv_motor_speed"))
    if speed is not None:
        row["vehicle_speed_kmh"] = speed
    elif row.get("vehicle_speed_kmh") in ("", None):
        row["vehicle_speed_kmh"] = 0


def _extract_wide_live_sample(parsed: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    """현재 패킷에서 실제로 포함된 CSV 필드와 값을 추출한다."""
    source = parsed
    samples = parsed.get("samples")
    if isinstance(samples, list) and samples:
        last = samples[-1]
        if isinstance(last, dict):
            source = last

    row = {field: "" for field in LOG_FIELDNAMES}
    row["stm_ms"] = _extract_stm_ms(source)
    _apply_sensor_map_to_row(row, source)
    present = {
        field
        for field in LOG_FIELDNAMES
        if field != "timestamp" and row.get(field) not in ("", None)
    }
    return row, present


def _row_with_timeline(payload: dict[str, Any], device_key: str) -> dict[str, Any]:
    row = _empty_wide_row(payload)
    row["timestamp"] = _format_recording_timestamp(_payload_time_to_datetime(payload))
    row["stm_ms"] = ""
    return row


def _apply_v2_timing_to_row(
    row: dict[str, Any],
    batch: dict[str, Any],
    sample: dict[str, Any],
) -> None:
    if _as_int(batch.get("schema_version")) != 2:
        return
    for field in (
        "schema_version",
        "pi_boot_id",
        "sender_instance_id",
        "batch_seq",
        "uart_rx_mono_ns",
        "pi_clock_source",
        "stm_epoch",
        "stm_wrap_count",
    ):
        value = batch.get(field)
        if value is not None:
            row[field] = value
    for field in (
        "stm_extended_ms",
        "pi_mono_fixed_5ms_ns",
        "pi_mono_est_ns",
    ):
        value = sample.get(field)
        if value is not None:
            row[field] = value


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
                    row["stm_ms"] = _extract_stm_ms(s)
                    _apply_v2_timing_to_row(row, parsed, s)
                    _apply_sensor_map_to_row(row, s)
                    rows.append(row)
                else:
                    row = _row_with_timeline(payload, device_key)
                    rows.append(row)
            return rows
        row = _row_with_timeline(payload, device_key)
        row["stm_ms"] = _extract_stm_ms(parsed)
        _apply_v2_timing_to_row(row, parsed, parsed)
        sample_ms = _as_int(parsed.get("stm_ms") if parsed.get("stm_ms") is not None else parsed.get("t"))
        _next_timestep(device_key, sample_ms)
        _apply_sensor_map_to_row(row, parsed)
        return [row]

    return [_empty_wide_row(payload)]


def _downsample_log_rows(
    rows: list[dict[str, Any]],
    *,
    device_key: str,
) -> list[dict[str, Any]]:
    """stm_ms 기준으로 LOG_TARGET_HZ 간격만 CSV에 남긴다 (기본 100Hz)."""
    if not rows or LOG_TARGET_HZ <= 0:
        return rows
    min_interval = max(0.0, LOG_MIN_INTERVAL_MS)
    if min_interval <= 0:
        return rows

    kept, last = downsample_rows(
        rows,
        log_last_stm_ms_by_device.get(device_key),
        min_interval_ms=min_interval,
        jitter_tolerance_ms=LOG_JITTER_TOLERANCE_MS,
        get_stamp=lambda row: _as_float(row.get("stm_ms")),
    )
    if last is not None:
        log_last_stm_ms_by_device[device_key] = float(last)
    return kept


def queue_log_rows(payload: dict[str, Any]) -> None:
    session = logging_session
    if not logging_enabled or session is None:
        return
    rows = build_log_rows(payload)
    if not rows:
        return
    device_key = _device_key_from_payload(payload)
    session.enqueue_rows(_downsample_log_rows(rows, device_key=device_key))


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
        sample_count = parse_sample_count(message)
        stm_timestamps_ms = extract_stm_timestamps_ms(message)
        record_traffic(sample_count, stm_timestamps_ms=stm_timestamps_ms)
        payload: dict[str, Any] = {
            "time": datetime.now(timezone.utc).isoformat(),
            "ip": addr[0],
            "port": addr[1],
            "message": message,
            "transport": "udp",
        }
        mark_device_seen(payload)
        record_platform_traffic(
            payload,
            sample_count,
            stm_timestamps_ms=stm_timestamps_ms,
        )
        payload = prepare_sensor_payload(payload)
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
    # 로거 종료 시에도 카메라 녹화는 반드시 같이 중지
    camera_result = await asyncio.to_thread(_stop_linked_camera_recording)
    if session is not None:
        await session.close()
        with suppress(Exception):
            _write_session_sync(
                session.session_id,
                {
                    "logging_stopped_at": datetime.now(timezone.utc).isoformat(),
                    "logging_stop_reason": "logger_shutdown",
                    "camera_ok": bool(camera_result.get("ok")),
                    "camera_stopped_at": str(camera_result.get("camera_stopped_at") or ""),
                    "camera_filename": str(camera_result.get("filename") or ""),
                    "camera_bytes": int(camera_result.get("file_size") or camera_result.get("bytes_written") or 0),
                    "camera_stop_message": str(camera_result.get("message") or ""),
                    "camera_active_after_stop": bool(camera_result.get("active")),
                },
            )
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


def _sample_count_from_ingest_body(body: Any) -> int:
    if isinstance(body, dict):
        declared = _as_int(body.get("sample_count"))
        samples = body.get("samples")
        if isinstance(samples, list) and samples:
            return max(1, len(samples))
        if declared is not None and declared > 0:
            return int(declared)
    return 1


def _ingest_channel(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    return str(body.get("channel") or "").strip().lower()


def _client_addr(request: Request) -> tuple[str, int]:
    client = request.client
    ip = client.host if client else "unknown"
    port = client.port if client else 0
    return ip, port


async def _handle_ingest_live(request: Request, body: Any) -> dict[str, Any]:
    """단일 진입점 (예전 방식): 파이 → /ingest 한 번.

    - 항상: 스냅샷/Hz + 실시간 서버로 포워드
    - 로깅 ON: 같은 페이로드를 CSV에도 기록
    /ingest/log 는 선택적 백필용이며, 파이가 평소에 쓸 필요 없음.
    """
    sample_count = _sample_count_from_ingest_body(body)
    stm_timestamps_ms = extract_stm_timestamps_ms(body)
    record_traffic(sample_count, stm_timestamps_ms=stm_timestamps_ms)

    ip, port = _client_addr(request)
    payload: dict[str, Any] = {
        "time": datetime.now(timezone.utc).isoformat(),
        "ip": ip,
        "port": port,
        "message": json.dumps(body, ensure_ascii=False),
        "transport": "http",
    }
    skip_forward = request.headers.get(FORWARD_HEADER) == "1"
    mark_device_seen(payload)
    record_platform_traffic(
        payload,
        sample_count,
        stm_timestamps_ms=stm_timestamps_ms,
    )
    payload = prepare_sensor_payload(payload)
    queue_log_rows(payload)
    schedule_live_processing(payload, skip_forward=skip_forward)
    return {"ok": True, "channel": "live", "samples": sample_count}


async def _handle_ingest_log(request: Request, body: Any) -> dict[str, Any]:
    """아카이브/CSV 전용: live UI·스냅샷·device_hz 미갱신. 항상 200."""
    sample_count = _sample_count_from_ingest_body(body)
    batch_seq = body.get("batch_seq") if isinstance(body, dict) else None
    enabled = bool(logging_enabled and logging_session is not None)
    logged = False
    if enabled:
        ip, port = _client_addr(request)
        payload: dict[str, Any] = {
            "time": datetime.now(timezone.utc).isoformat(),
            "ip": ip,
            "port": port,
            "message": json.dumps(body, ensure_ascii=False),
            "transport": "http_log",
        }
        payload = prepare_sensor_payload(payload, update_snapshot=False)
        queue_log_rows(payload)
        logged = True
    return {
        "ok": True,
        "channel": "log",
        "samples": sample_count,
        "batch_seq": batch_seq,
        "logged": logged,
        "logging_enabled": bool(logging_enabled),
    }


@app.post("/ingest")
async def ingest_http(request: Request) -> Any:
    """실시간 경로 (/ingest). channel=log 이면 로그 경로로 넘긴다."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if _ingest_channel(body) == "log":
        return await _handle_ingest_log(request, body)
    return await _handle_ingest_live(request, body)


@app.post("/ingest/log")
async def ingest_log_http(request: Request) -> Any:
    """로깅/백필 경로. 인증 없음, 항상 200. sync 메타 강제 없음."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _handle_ingest_log(request, body)


@app.get("/api/ingest/sequence-status")
def ingest_sequence_status(device: str, pi_boot_id: str) -> dict[str, Any]:
    return {
        "ok": True,
        **ingest_v2_store.sequence_status(device.strip(), pi_boot_id.strip()),
    }


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
            sample_count = parse_sample_count(message)
            stm_timestamps_ms = extract_stm_timestamps_ms(message)
            record_traffic(sample_count, stm_timestamps_ms=stm_timestamps_ms)
            payload: dict[str, Any] = {
                "time": datetime.now(timezone.utc).isoformat(),
                "ip": ip,
                "port": port,
                "message": message,
                "transport": "tcp",
            }
            mark_device_seen(payload)
            record_platform_traffic(
                payload,
                sample_count,
                stm_timestamps_ms=stm_timestamps_ms,
            )
            payload = prepare_sensor_payload(payload)
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
        "hz_1s": round(hz_last(1.0), 1),
        "hz_5s_avg": round(hz_last(5.0), 1),
        "hz_basis": "stm_ms",
        "device_hz": {
            "esp32": {
                "hz_1s": round(hz_last_for_platform("esp32", 1.0), 1),
                "hz_5s_avg": round(hz_last_for_platform("esp32", 5.0), 1),
            },
            "raspberry_pi": {
                "hz_1s": round(hz_last_for_platform("raspberry_pi", 1.0), 1),
                "hz_5s_avg": round(hz_last_for_platform("raspberry_pi", 5.0), 1),
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
        "log_target_hz": LOG_TARGET_HZ,
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


def _logging_preflight_payload() -> dict[str, Any]:
    connected = online_device_count()
    disk_ok, disk = _disk_free_ok(LOG_DIR)
    current_hz = round(hz_last(1.0), 1)
    online_keys = sorted(device_last_seen_epoch_ms, key=device_last_seen_epoch_ms.get, reverse=True)
    device_key = online_keys[0] if online_keys else ""
    present_fields = latest_wide_fields_by_device.get(device_key, set())
    # v2 timing metadata is optional so legacy clients keep passing preflight.
    required_fields = set(LOG_FIELDNAMES) - {"timestamp"} - V2_TIMING_FIELDNAMES - {"can1", "can2"}
    missing_fields = sorted(required_fields - present_fields)

    stm_state = latest_stm_state_by_device.get(device_key) or {}
    stm_age_sec = (
        max(0.0, monotonic() - float(stm_state.get("changed_at", 0.0)))
        if stm_state
        else None
    )
    stm_advancing = stm_age_sec is not None and stm_age_sec <= 2.0
    camera = _linked_camera_recording_status()
    camera_ok = not LINK_CAMERA_RECORDING or bool(camera.get("ok"))
    offset_count = len(device_offsets.get(device_key) or {})

    checks = [
        {
            "id": "device",
            "label": "장비 연결",
            "ok": connected > 0,
            "level": "ok" if connected > 0 else "bad",
            "detail": f"{connected}대 연결" if connected > 0 else "수신 장비 없음",
        },
        {
            "id": "rate",
            "label": "수신 주파수",
            "ok": current_hz > 0,
            "level": "ok" if current_hz > 0 else "bad",
            "detail": f"{current_hz:.1f} Hz",
        },
        {
            "id": "fields",
            "label": "센서 필드",
            "ok": not missing_fields,
            "level": "ok" if not missing_fields else "warn",
            "detail": (
                f"{len(present_fields)}/{len(required_fields)} 수신"
                if not missing_fields
                else f"누락: {', '.join(missing_fields)}"
            ),
        },
        {
            "id": "stm",
            "label": "STM 시간",
            "ok": stm_advancing,
            "level": "ok" if stm_advancing else "bad",
            "detail": (
                f"증가 중 · {float(stm_state.get('value', 0)):.0f} ms"
                if stm_advancing
                else "변화 없음 또는 미수신"
            ),
        },
        {
            "id": "disk",
            "label": "저장 공간",
            "ok": disk_ok,
            "level": "ok" if disk_ok else "bad",
            "detail": f"{disk['free_bytes'] / (1024 ** 3):.1f} GB 여유",
        },
        {
            "id": "camera",
            "label": "카메라 서버",
            "ok": camera_ok,
            "level": "ok" if camera_ok else "bad",
            "detail": (
                "연동 준비"
                if camera_ok and LINK_CAMERA_RECORDING
                else ("연동 안 함" if not LINK_CAMERA_RECORDING else str(camera.get("message") or "응답 없음"))
            ),
        },
        {
            "id": "offset",
            "label": "오프셋",
            "ok": offset_count > 0,
            "level": "ok" if offset_count > 0 else "warn",
            "detail": f"{offset_count}개 적용" if offset_count > 0 else "미적용",
        },
    ]
    critical_ok = all(check["ok"] for check in checks if check["level"] != "warn")
    return {
        "ok": True,
        "ready": critical_ok,
        "device": device_key,
        "checks": checks,
        "missing_fields": missing_fields,
        "camera": camera,
        "disk": disk,
    }


@app.get("/api/logging/preflight")
def logging_preflight() -> dict[str, Any]:
    return _logging_preflight_payload()


@app.get("/api/offset/verify")
def offset_verify() -> dict[str, Any]:
    now_mono = monotonic()
    window_sec = 3.0
    items: list[dict[str, Any]] = []
    device_results: list[dict[str, Any]] = []

    for device, offsets in device_offsets.items():
        history = offset_sample_history_by_device.get(device) or deque()
        samples = [snapshot for ts, snapshot in history if ts >= now_mono - window_sec]
        device_items: list[dict[str, Any]] = []
        for path, offset in offsets.items():
            values = [
                float(snapshot[path]) - float(offset)
                for snapshot in samples
                if path in snapshot
            ]
            if not values:
                continue
            target = 1.0 if path == "car.accel.z" else 0.0
            mean = sum(values) / len(values)
            variance = sum((value - mean) ** 2 for value in values) / len(values)
            stddev = math.sqrt(variance)
            is_accel = path.startswith("car.accel.")
            error_tolerance = 0.04 if is_accel else 0.8
            stability_tolerance = 0.05 if is_accel else 1.0
            passed = (
                len(values) >= 5
                and abs(mean - target) <= error_tolerance
                and stddev <= stability_tolerance
            )
            item = {
                "device": device,
                "path": path,
                "label": sensor_offset_label(path),
                "unit": sensor_offset_unit(path),
                "samples": len(values),
                "target": target,
                "mean": round(mean, 4),
                "error": round(mean - target, 4),
                "stddev": round(stddev, 4),
                "passed": passed,
            }
            items.append(item)
            device_items.append(item)

        device_results.append(
            {
                "device": device,
                "sample_count": max((item["samples"] for item in device_items), default=0),
                "passed": bool(device_items) and all(item["passed"] for item in device_items),
                "items": device_items,
            }
        )

    return {
        "ok": True,
        "window_sec": window_sec,
        "passed": bool(items) and all(item["passed"] for item in items),
        "devices": device_results,
        "items": items,
        "message": "오프셋을 먼저 적용하세요." if not device_offsets else "",
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
        "label": session_status.get("label", ""),
        "output_dir": str(LOG_DIR),
        "file_bytes": session_status.get("file_bytes", 0),
        "compressed": session_status.get("compressed", LOG_DOWNLOAD_GZIP),
        "log_target_hz": LOG_TARGET_HZ,
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
async def logging_start(request: Request) -> Response:
    global logging_enabled, logging_session, last_sample_ms_by_device, sample_index_by_device, logging_started_epoch_ms, wheel_last_sample_ms_by_device, log_last_stm_ms_by_device
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

    body: dict[str, Any] = {}
    with suppress(Exception):
        raw_body = await request.json()
        if isinstance(raw_body, dict):
            body = raw_body
    log_label = _sanitize_log_label(str(body.get("label") or body.get("name") or ""))

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
    log_last_stm_ms_by_device = {}
    logging_started_epoch_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    logging_session = CsvLogSession(started_epoch_ms=logging_started_epoch_ms, label=log_label)
    command_id = f"cmd_{secrets.token_urlsafe(18)}"
    recording_id = f"rec_{secrets.token_urlsafe(18)}"
    video_filename = _camera_recording_filename(logging_session.session_id)
    # 이전 세션 Pi sync report가 없어도 재시작 가능해야 함 (정밀 sync 미사용 포함)
    camera_sync_store.abandon_pending(device=CAMERA_SYNC_DEVICE)
    try:
        camera_sync_store.create_command(
            command_id=command_id,
            recording_id=recording_id,
            session_id=logging_session.session_id,
            device=CAMERA_SYNC_DEVICE,
            video_filename=video_filename,
        )
        logging_session.recording_id = recording_id
        logging_session.sync_command_id = command_id
    except PendingCommandExists:
        # 경합 시 한 번 더 비우고 재시도. 그래도 실패하면 sync만 건너뛰고 로깅 진행.
        camera_sync_store.abandon_pending(device=CAMERA_SYNC_DEVICE)
        try:
            camera_sync_store.create_command(
                command_id=command_id,
                recording_id=recording_id,
                session_id=logging_session.session_id,
                device=CAMERA_SYNC_DEVICE,
                video_filename=video_filename,
            )
            logging_session.recording_id = recording_id
            logging_session.sync_command_id = command_id
        except PendingCommandExists:
            logging_session.recording_id = recording_id
            logging_session.sync_command_id = ""
    logging_session.start()
    logging_enabled = True
    camera_result = await asyncio.to_thread(
        _start_linked_camera_recording,
        logging_session.session_id,
        recording_id,
    )
    video_filename = str(camera_result.get("filename") or video_filename)
    if logging_session.sync_command_id:
        camera_sync_store.activate_command(logging_session.sync_command_id, video_filename)
    sync_path = _write_session_sync(
        logging_session.session_id,
        {
            "logging_started_at": datetime.fromtimestamp(
                logging_started_epoch_ms / 1000.0, tz=timezone.utc
            ).isoformat(),
            "logging_started_epoch_ms": logging_started_epoch_ms,
            "csv_prefix": f"sensor_log_{logging_session.session_id}",
            "camera_prefix": f"camera-{logging_session.session_id}",
            "camera_mode": str(camera_result.get("record_mode") or "rtsp"),
            "camera_capture": "camera",
            "camera_started_at": str(camera_result.get("started_at") or ""),
            "camera_filename": video_filename,
            "camera_ok": bool(camera_result.get("ok") or camera_result.get("armed")),
            "camera_armed": bool(camera_result.get("armed")),
            "camera_message": str(camera_result.get("message") or camera_result.get("start_error") or ""),
            "recording_id": recording_id,
            "sync_command_id": logging_session.sync_command_id or command_id,
            "sync_device": CAMERA_SYNC_DEVICE,
            "pi_sync_status": "pending" if logging_session.sync_command_id else "skipped",
            "offset_sec_hint": None,
            "sync_rule": "pi_boottime_pending_recording_first_rtp",
            "note": "서버 HTTP 수신 시각을 영상 첫 프레임 시각으로 사용하지 않음",
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
                "label": logging_session.label,
                "recording_id": recording_id,
                "command_id": command_id,
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
    stopped_at = datetime.now(timezone.utc).isoformat()

    # CSV flush보다 카메라를 먼저 내려 동시에 끊기게 한다.
    camera_result = await asyncio.to_thread(_stop_linked_camera_recording)
    # Pi sync report를 기다리지 않음 — pending이 남으면 다음 시작이 막힘
    if session is not None and session.sync_command_id:
        camera_sync_store.abandon_pending(command_id=session.sync_command_id)
    else:
        camera_sync_store.abandon_pending(device=CAMERA_SYNC_DEVICE)

    if session is None:
        return Response(
            content=json.dumps(
                {
                    "ok": False,
                    "message": "저장할 데이터가 없습니다.",
                    "camera_recording": camera_result,
                    "camera_stopped": bool(camera_result.get("ok")),
                },
                ensure_ascii=False,
            ),
            media_type="application/json",
            status_code=200,
        )

    await session.close()
    rtp_finalize: dict[str, Any] = {}
    camera_rtp_sync = camera_result.get("rtp_sync")
    if isinstance(camera_rtp_sync, dict) and camera_rtp_sync:
        with suppress(Exception):
            rtp_finalize = _finalize_rtp_sync_sidecar(
                session.session_id,
                str(
                    camera_result.get("filename")
                    or _camera_recording_filename(session.session_id)
                ),
                camera_rtp_sync,
            )

    sync_camera = {
        "logging_stopped_at": stopped_at,
        "recording_id": session.recording_id,
        "sync_command_id": session.sync_command_id,
        "rtp_sync": camera_rtp_sync if isinstance(camera_rtp_sync, dict) else {},
        "rtp_mapping_ready": bool(rtp_finalize.get("mapping_ready")),
        "video_sync_sidecar": str(rtp_finalize.get("path") or ""),
        "camera_ok": bool(camera_result.get("ok")),
        "camera_stopped_at": str(camera_result.get("camera_stopped_at") or stopped_at if camera_result.get("ok") else ""),
        "camera_filename": str(camera_result.get("filename") or ""),
        "camera_segments": list(camera_result.get("segments") or []),
        "camera_bytes": int(camera_result.get("file_size") or camera_result.get("bytes_written") or 0),
        "camera_stop_message": str(camera_result.get("message") or ""),
        "camera_active_after_stop": bool(camera_result.get("active") or camera_result.get("armed")),
        "camera_stop_attempts": int(camera_result.get("attempts") or 0),
        "first_sensor_pi_mono_est_ns": session.first_pi_mono_est_ns,
        "last_sensor_pi_mono_est_ns": session.last_pi_mono_est_ns,
        "offset_sec_hint": None,
        "sync_rule": "pi_boottime_pending_recording_first_rtp",
        "time_conversion_formula": (
            "video_frame_pi_ns = rtp_anchor_pi_ns + "
            "rtp_delta(recording_first_rtp + video_pts_ns*90000/1e9, "
            "rtp_anchor)*1e9/90000"
        ),
    }

    if session.rows_written < 1:
        sync_path = _write_session_sync(session.session_id, sync_camera)
        return Response(
            content=json.dumps(
                {
                    "ok": False,
                    "message": "저장할 데이터가 없습니다.",
                    "camera_recording": camera_result,
                    "camera_stopped": bool(camera_result.get("ok")),
                    "session_sync": str(sync_path),
                },
                ensure_ascii=False,
            ),
            media_type="application/json",
            status_code=200,
        )
    download_token = secrets.token_urlsafe(16)
    download_payload = session.build_download_payload()
    report = session.build_report(stopped_at=stopped_at, camera_result=camera_result)
    download_payload["report"] = report
    _store_completed_download(download_token, download_payload)
    sync_path = _write_session_sync(
        session.session_id,
        {
            **sync_camera,
            "csv_filename": download_payload.get("filename", ""),
            "csv_rows": session.rows_written,
            "report": report,
        },
    )
    cam_ok = bool(camera_result.get("ok"))
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
                "camera_stopped": cam_ok,
                "report": report,
                "message": (
                    None
                    if cam_ok
                    else f"로깅은 중지됐지만 카메라 녹화가 안 멈췄을 수 있습니다: {camera_result.get('message')}"
                ),
                "session_sync": str(sync_path),
            },
            ensure_ascii=False,
        ),
        media_type="application/json",
        status_code=200,
    )


@app.get("/api/logging/files/calendar")
def logging_files_calendar() -> dict[str, Any]:
    return _logging_files_calendar()


@app.get("/api/logging/files")
def logging_files_by_day(
    year: int | None = None,
    month: int | None = None,
    day: int | None = None,
    date: str | None = None,
) -> dict[str, Any]:
    ymd = ""
    if date:
        digits = re.sub(r"\D", "", str(date))
        if len(digits) >= 8:
            ymd = _normalize_ymd(digits[:4], digits[4:6], digits[6:8]) or ""
    elif year is not None and month is not None and day is not None:
        ymd = _normalize_ymd(year, month, day) or ""
    if not ymd:
        return {"ok": False, "message": "년/월/일을 선택하세요.", "files": [], "date": ""}
    files = _list_logging_files_for_ymd(ymd)
    return {
        "ok": True,
        "timezone": "Asia/Seoul",
        "date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}",
        "ymd": ymd,
        "count": len(files),
        "files": files,
        "zip_url": f"/api/logging/files/day-zip?date={ymd}" if files else "",
    }


@app.get("/api/logging/files/download/{filename:path}")
def logging_file_download(filename: str) -> Response:
    path = _safe_log_download_path(filename)
    if path is None:
        return Response(
            content=json.dumps({"ok": False, "message": "파일을 찾을 수 없습니다."}, ensure_ascii=False),
            media_type="application/json",
            status_code=404,
        )
    compressed = path.name.endswith(".gz")
    return FileResponse(
        path=path,
        media_type="application/gzip" if compressed else "text/csv; charset=utf-8",
        filename=path.name,
        headers={"X-Download-Bytes": str(path.stat().st_size)},
    )


@app.get("/api/logging/files/day-zip")
def logging_files_day_zip(
    background_tasks: BackgroundTasks,
    year: int | None = None,
    month: int | None = None,
    day: int | None = None,
    date: str | None = None,
) -> Response:
    ymd = ""
    if date:
        digits = re.sub(r"\D", "", str(date))
        if len(digits) >= 8:
            ymd = _normalize_ymd(digits[:4], digits[4:6], digits[6:8]) or ""
    elif year is not None and month is not None and day is not None:
        ymd = _normalize_ymd(year, month, day) or ""
    if not ymd:
        return Response(
            content=json.dumps({"ok": False, "message": "년/월/일을 선택하세요."}, ensure_ascii=False),
            media_type="application/json",
            status_code=400,
        )
    zip_path = _build_day_zip(ymd)
    if zip_path is None or not zip_path.is_file():
        return Response(
            content=json.dumps({"ok": False, "message": "해당 날짜 로그 파일이 없습니다."}, ensure_ascii=False),
            media_type="application/json",
            status_code=404,
        )

    def _cleanup(path: str) -> None:
        with suppress(OSError):
            Path(path).unlink(missing_ok=True)

    background_tasks.add_task(_cleanup, str(zip_path))
    return FileResponse(
        path=zip_path,
        media_type="application/zip",
        filename=zip_path.name,
        headers={"X-Download-Bytes": str(zip_path.stat().st_size)},
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
    global device_offsets, _offsets_fl_rr_label_migrated
    try:
        body = await request.json()
    except Exception:
        body = {}

    if not isinstance(body, dict):
        body = {}

    device_key = str(body.get("device", "")).strip()
    group = str(body.get("group") or body.get("target") or "all").strip().lower() or "all"
    sensor_values = body.get("sensor_values")

    if isinstance(sensor_values, dict) and sensor_values:
        if not device_key:
            return {"ok": False, "message": "sensor_values를 직접 지정할 때는 device가 필요합니다."}
        manual_snapshot: dict[str, float] = {}
        for sensor_path, value in sensor_values.items():
            value_f = _as_float(value)
            if value_f is None:
                continue
            manual_snapshot[str(sensor_path)] = value_f
        device_offsets[device_key] = merge_device_offsets(
            device_offsets.get(device_key),
            manual_snapshot,
            group=group,
        )
        # 신규 보정은 FL↔RR 스왑 이후 스냅샷 기준
        _offsets_fl_rr_label_migrated = True
        from offset_shared import public_offset_status

        status = public_offset_status(device_offsets)
        return {
            "ok": True,
            "mode": "manual",
            "group": group,
            "device": device_key,
            "offsets": device_offsets.get(device_key, {}),
            **status,
        }

    if device_key:
        snapshot = latest_sensor_snapshot_by_device.get(device_key)
        if not snapshot:
            return {"ok": False, "message": f"{device_key}의 최신 센서 데이터가 없어 오프셋을 잡을 수 없습니다."}
        partial = snapshot_to_offsets(snapshot, group=group)
        if not partial:
            return {
                "ok": False,
                "message": f"{device_key}에 {group} 보정용 센서 값이 없습니다. UART 수신을 확인하세요.",
            }
        device_offsets[device_key] = merge_device_offsets(
            device_offsets.get(device_key),
            snapshot,
            group=group,
        )
        _offsets_fl_rr_label_migrated = True
        from offset_shared import public_offset_status

        return {
            "ok": True,
            "mode": "capture_latest",
            "group": group,
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
        partial = snapshot_to_offsets(snapshot, group=group)
        if not partial:
            continue
        device_offsets[key] = merge_device_offsets(device_offsets.get(key), snapshot, group=group)
        calibrated_devices.append(key)

    if not calibrated_devices:
        return {"ok": False, "message": "캘리브레이션 가능한 센서 데이터가 없습니다."}

    _offsets_fl_rr_label_migrated = True
    from offset_shared import public_offset_status

    return {
        "ok": True,
        "mode": "capture_latest_all",
        "group": group,
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
    from offset_shared import public_offset_status

    if device_key:
        if device_key in device_offsets:
            del device_offsets[device_key]
        return {
            "ok": True,
            "device": device_key,
            "message": f"{device_key}의 오프셋이 초기화되었습니다.",
            **public_offset_status(device_offsets),
        }

    device_offsets.clear()
    return {
        "ok": True,
        "message": "모든 오프셋이 초기화되었습니다.",
        **public_offset_status(device_offsets),
    }