#!/usr/bin/env python3
"""CSV + video analysis server: sync playback with ±15s sensor charts."""

from __future__ import annotations

import csv
import gzip
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
SERVER_ROOT = BASE_DIR.parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

LOG_DIR = Path(os.getenv("LOG_DIR", str(SERVER_ROOT / "udp_logger" / "logs")))
CAM_DIR = Path(
    os.getenv(
        "CAMERA_RECORDINGS_DIR",
        str(SERVER_ROOT / "udp_realtime" / "camera_recordings"),
    )
)

MAX_SESSION_BYTES = int(os.getenv("ANALYSIS_MAX_SESSION_BYTES", str(512 * 1024 * 1024)))
MAX_SESSIONS = int(os.getenv("ANALYSIS_MAX_SESSIONS", "20"))
DEFAULT_WINDOW_SEC = 15.0
MAX_POINTS = 2000
# Import/upload 시 fragmented MP4 → faststart remux만 (재인코딩 proxy는 기본 OFF — 1GB+에서 수십 분 소요)
PLAYBACK_PROXY_ENABLE = os.getenv("ANALYSIS_PLAYBACK_PROXY", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
PLAYBACK_PROXY_MIN_BYTES = int(
    os.getenv("ANALYSIS_PROXY_MIN_BYTES", str(80 * 1024 * 1024))
)
PLAYBACK_PROXY_WIDTH = max(320, int(os.getenv("ANALYSIS_PROXY_WIDTH", "1280")))
PLAYBACK_PROXY_CRF = max(18, min(35, int(os.getenv("ANALYSIS_PROXY_CRF", "28"))))
PLAYBACK_FFMPEG_TIMEOUT_SEC = max(
    60, int(os.getenv("ANALYSIS_FFMPEG_TIMEOUT_SEC", "1800"))
)
SKIP_COLUMNS = {
    "timestamp",
    "stm_ms",
    "stm_time_ns",
    "monotonic_ns",
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
    "motor_inverter_mode",
}
VEHICLE_COLS = (
    "x_g",
    "y_g",
    "z_g",
    "ecu_temp",
    "FL",
    "FR",
    "RL",
    "RR",
    "steering_angle",
    "steering_speed",
    "inv_temp_igt",
    "inv_temp_motor",
    "inv_motor_speed",
    "vehicle_speed_kmh",
    "inv_dc_current",
    "inv_voltage",
    "bms_charge",
    "bms_voltage",
    "bms_current",
    "bms_ccl",
    "bms_dcl",
    "bms_temp_maxvalue",
    "bms_capacity",
)

TIMESTAMP_RE = re.compile(
    r"^(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?$"
)
TIME_OCR_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})")

app = FastAPI(title="AFA Analysis Server")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_sessions: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()

try:
    import sys

    sys.path.insert(0, str(BASE_DIR.parent))
    from afa_auth import install_auth

    install_auth(app)
except Exception:
    pass

try:
    from nav_config import nav_context
except Exception:
    def nav_context(current: str, request: Any = None) -> dict[str, Any]:  # type: ignore[misc]
        return {
            "nav": {
                "logger": "",
                "realtime": "",
                "camera": "",
                "analysis": "",
                "current": current,
            }
        }

from postprocess import (
    CHART_PRESETS,
    build_decimated_cache,
    compare_segments,
    detect_drive_starts,
    find_segment,
    load_decimated_cache,
    load_events,
    load_kpi,
    load_segments,
    run as run_postprocess,
)
from vehicle_spec import vehicle_public_spec


def _window_cache_for_parsed(parsed: dict[str, Any]) -> dict[str, Any] | None:
    """재생 중 /window 가 밀리지 않도록 경량 다운샘플 캐시 (~25Hz)."""
    try:
        return build_decimated_cache(parsed, target_hz=25.0)
    except Exception:
        return None


def _parse_timestamp(raw: str) -> float | None:
    text = (raw or "").strip()
    if not text:
        return None
    m = TIMESTAMP_RE.match(text)
    if m:
        year, month, day, hour, minute, second, frac = m.groups()
        micro = int((frac or "0").ljust(6, "0")[:6])
        try:
            dt = datetime(
                int(year),
                int(month),
                int(day),
                int(hour),
                int(minute),
                int(second),
                micro,
            )
            return dt.timestamp()
        except ValueError:
            return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.timestamp()
    except ValueError:
        return None


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _session_dir(session_id: str) -> Path:
    return UPLOAD_DIR / session_id


def _cleanup_old_sessions() -> None:
    # Only drop oldest in-memory sessions. Never wipe upload dirs on unload/restart.
    with _lock:
        items = sorted(
            ((sid, meta) for sid, meta in _sessions.items()),
            key=lambda item: float(item[1].get("created_at", 0)),
        )
        while len(items) > MAX_SESSIONS:
            sid, _ = items.pop(0)
            _sessions.pop(sid, None)


def _load_csv(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        if not reader.fieldnames:
            raise ValueError("CSV 헤더가 없습니다.")
        columns = [c for c in reader.fieldnames if c and c.strip()]
        if "timestamp" not in columns:
            raise ValueError("CSV에 timestamp 컬럼이 필요합니다.")

        numeric_cols = [c for c in columns if c not in SKIP_COLUMNS]
        records: list[tuple[dict[str, str], float]] = []
        for row in reader:
            t = _parse_timestamp(str(row.get("timestamp") or ""))
            if t is None:
                continue
            records.append((dict(row), t))

    if not records:
        raise ValueError("유효한 timestamp 행이 없습니다.")

    boot_ids = {
        str(row.get("pi_boot_id") or "").strip()
        for row, _ in records
        if str(row.get("pi_boot_id") or "").strip()
    }
    can_restore_v2_order = (
        len(boot_ids) == 1
        and all(_safe_float(row.get("batch_seq")) is not None for row, _ in records)
        and all(_safe_float(row.get("pi_mono_est_ns")) is not None for row, _ in records)
    )
    time_source = "server_timestamp"
    if can_restore_v2_order:
        records.sort(
            key=lambda item: (
                int(float(str(item[0].get("batch_seq") or "0"))),
                int(float(str(item[0].get("stm_extended_ms") or "0"))),
            )
        )
        first_pi_ns = int(float(str(records[0][0]["pi_mono_est_ns"])))
        first_server_time = records[0][1]
        times = [
            first_server_time
            + (
                int(float(str(row["pi_mono_est_ns"])))
                - first_pi_ns
            )
            / 1_000_000_000.0
            for row, _ in records
        ]
        time_source = "pi_mono_est_ns_batch_seq_order"
    else:
        times = [t for _, t in records]

    series: dict[str, list[float | None]] = {c: [] for c in numeric_cols}
    raw_timestamps: list[str] = []
    for row, _ in records:
        raw_timestamps.append(str(row.get("timestamp") or ""))
        for col in numeric_cols:
            series[col].append(_safe_float(row.get(col)))

    usable_cols = []
    for col in numeric_cols:
        if any(v is not None for v in series[col]):
            usable_cols.append(col)
        else:
            series.pop(col, None)

    return {
        "t0": times[0],
        "t_end": times[-1],
        "duration_sec": max(0.0, times[-1] - times[0]),
        "row_count": len(times),
        "columns": usable_cols,
        "times": times,
        "series": series,
        "raw_timestamps": raw_timestamps,
        "time_source": time_source,
    }


def _ffmpeg_bin() -> str:
    local = BASE_DIR / "bin" / "ffmpeg"
    if local.is_file():
        return str(local)
    shared = BASE_DIR.parent / "udp_realtime" / "bin" / "ffmpeg"
    if shared.is_file():
        return str(shared)
    return shutil.which("ffmpeg") or ""


def _video_media_type(ext: str) -> str:
    return {
        ".mp4": "video/mp4",
        ".avi": "video/x-msvideo",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".mkv": "video/x-matroska",
    }.get(ext.lower(), "application/octet-stream")


def _run_ffmpeg(cmd: list[str], *, timeout_sec: int) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return False, f"ffmpeg timed out after {timeout_sec}s"
    except OSError as exc:
        return False, str(exc)
    err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        return False, err or f"ffmpeg exited {proc.returncode}"
    return True, err


def _remux_faststart(src: Path, dest: Path) -> tuple[bool, str]:
    """Copy-remux to put moov at front (fixes RTSP fragmented MP4 browser playback)."""
    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        return False, "ffmpeg not found"
    if dest.exists():
        dest.unlink(missing_ok=True)
    ok, err = _run_ffmpeg(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-an",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(dest),
        ],
        timeout_sec=PLAYBACK_FFMPEG_TIMEOUT_SEC,
    )
    if not ok or not dest.is_file() or dest.stat().st_size <= 0:
        dest.unlink(missing_ok=True)
        return False, err or "remux produced empty file"
    return True, "faststart remux"


def _encode_watch_proxy(src: Path, dest: Path) -> tuple[bool, str]:
    """Lower-res H.264 for continuous browser playback of large recordings."""
    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        return False, "ffmpeg not found"
    if dest.exists():
        dest.unlink(missing_ok=True)
    scale = f"scale='min({PLAYBACK_PROXY_WIDTH},iw)':-2"
    ok, err = _run_ffmpeg(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-an",
            "-vf",
            scale,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(PLAYBACK_PROXY_CRF),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(dest),
        ],
        timeout_sec=PLAYBACK_FFMPEG_TIMEOUT_SEC,
    )
    if not ok or not dest.is_file() or dest.stat().st_size <= 0:
        dest.unlink(missing_ok=True)
        return False, err or "proxy encode produced empty file"
    return True, f"proxy {PLAYBACK_PROXY_WIDTH}p crf{PLAYBACK_PROXY_CRF}"


def _playable_sidecar_path(src: Path) -> Path | None:
    if src.suffix.lower() not in {".mp4", ".mov", ".mkv", ".m4v"}:
        return None
    # camera-ID.play.mp4 / camera-ID.watch.mp4 (원본 stem 기준)
    stem = src.stem
    if stem.endswith(".play") or stem.endswith(".watch"):
        stem = stem.rsplit(".", 1)[0]
    return src.with_name(f"{stem}.play.mp4")


def _watch_sidecar_path(src: Path) -> Path | None:
    if src.suffix.lower() not in {".mp4", ".mov", ".mkv", ".m4v"}:
        return None
    stem = src.stem
    if stem.endswith(".play") or stem.endswith(".watch"):
        stem = stem.rsplit(".", 1)[0]
    return src.with_name(f"{stem}.watch.mp4")


def _link_or_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        dest.unlink(missing_ok=True)
    try:
        os.link(src, dest)
    except OSError:
        try:
            dest.symlink_to(src.resolve())
        except OSError:
            shutil.copy2(src, dest)


def _sidecar_usable(src: Path, sidecar: Path) -> bool:
    if not sidecar.is_file() or sidecar.stat().st_size < 1024:
        return False
    # remux 산출물이 원본보다 너무 오래됐으면 무시
    try:
        return sidecar.stat().st_mtime + 1.0 >= src.stat().st_mtime
    except OSError:
        return False


def _prepare_playback_video(src: Path, dest_dir: Path) -> dict[str, Any]:
    """
    Browser-friendly video for the session.
    Default: faststart remux only (no re-encode). Cache as <name>.play.mp4 next to
    the recording so a 1.8GB file is remuxed once, then hardlinked into sessions.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    play_path = dest_dir / "video.mp4"
    proxy_path = dest_dir / "_proxy.mp4"
    ext = src.suffix.lower() or ".mp4"
    src_bytes = src.stat().st_size if src.is_file() else 0
    prep: dict[str, Any] = {
        "mode": "passthrough",
        "source_bytes": src_bytes,
        "play_bytes": 0,
        "message": "",
        "ok": False,
    }

    def _finish(path: Path, mode: str, message: str) -> dict[str, Any]:
        proxy_path.unlink(missing_ok=True)
        prep["mode"] = mode
        prep["message"] = message
        prep["ok"] = True
        prep["play_bytes"] = path.stat().st_size if path.is_file() else 0
        prep["path"] = path
        prep["media_type"] = _video_media_type(path.suffix)
        return prep

    def _install(playable: Path, mode: str, message: str) -> dict[str, Any]:
        _link_or_copy(playable, play_path)
        return _finish(play_path, mode, message)

    if not src.is_file():
        raise FileNotFoundError(f"영상 없음: {src}")

    sidecar = _playable_sidecar_path(src)
    watch = _watch_sidecar_path(src)

    # 2시간급: 저해상도 .watch.mp4 있으면 그걸 재생 (용량↓ 연속재생↑)
    if watch and _sidecar_usable(src, watch):
        return _install(watch, "watch", "기존 .watch.mp4 사용 (저해상도·연속재생)")

    # 이미 remux된 .play.mp4 있으면 즉시 하드링크 (수 초)
    if sidecar and _sidecar_usable(src, sidecar):
        return _install(sidecar, "cached", "기존 .play.mp4 사용 (즉시)")

    if not _ffmpeg_bin():
        _link_or_copy(src, dest_dir / f"video{ext}")
        return _finish(dest_dir / f"video{ext}", "passthrough", "ffmpeg 없음 · 원본 링크")

    # remux 대상: 녹화 폴더에 쓰기 가능하면 사이드카에 캐시 (다음 import 즉시)
    remux_dest = sidecar if sidecar is not None else (dest_dir / "_remux.mp4")
    can_write_sidecar = False
    if sidecar is not None:
        try:
            can_write_sidecar = os.access(src.parent, os.W_OK)
        except OSError:
            can_write_sidecar = False
    if not can_write_sidecar:
        remux_dest = dest_dir / "_remux.mp4"

    remux_ok, remux_msg = _remux_faststart(src, remux_dest)
    work = remux_dest if remux_ok else src
    work_bytes = work.stat().st_size if work.is_file() else 0
    want_proxy = PLAYBACK_PROXY_ENABLE and work_bytes >= PLAYBACK_PROXY_MIN_BYTES

    if want_proxy:
        proxy_ok, proxy_msg = _encode_watch_proxy(work, proxy_path)
        if proxy_ok:
            if play_path.exists():
                play_path.unlink(missing_ok=True)
            proxy_path.replace(play_path)
            note = proxy_msg
            if remux_ok:
                note = f"{remux_msg} → {proxy_msg}"
            return _finish(play_path, "proxy", note)
        prep["proxy_error"] = proxy_msg

    if remux_ok:
        return _install(remux_dest, "remux", remux_msg)

    _link_or_copy(src, dest_dir / f"video{ext}")
    return _finish(
        dest_dir / f"video{ext}",
        "passthrough",
        f"remux 실패 · 원본 링크 ({remux_msg})",
    )


def _extract_video_frame(video_path: Path, at_sec: float, out_path: Path) -> bool:
    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        return False
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{max(0.0, at_sec):g}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    return proc.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0


def _ocr_clock_hms(img_path: Path) -> tuple[int, int, int] | None:
    try:
        from PIL import Image, ImageEnhance, ImageOps
        import pytesseract
    except ImportError:
        return None

    img = Image.open(img_path)
    width, height = img.size
    crop_boxes = [
        (0, int(height * 0.68), int(width * 0.52), height),
        (0, int(height * 0.72), int(width * 0.45), height),
        (0, int(height * 0.60), int(width * 0.58), int(height * 0.96)),
    ]
    ocr_configs = [
        "--psm 7 -c tessedit_char_whitelist=0123456789:",
        "--psm 6 -c tessedit_char_whitelist=0123456789:",
        "--psm 11",
    ]

    for box in crop_boxes:
        crop = img.crop(box)
        crop = crop.resize(
            (max(1, crop.width * 2), max(1, crop.height * 2)),
            Image.Resampling.LANCZOS,
        )
        gray = crop.convert("L")
        gray = ImageOps.autocontrast(gray)
        gray = ImageEnhance.Contrast(gray).enhance(2.2)
        variants = [gray, ImageOps.invert(gray)]
        for variant in variants:
            for cfg in ocr_configs:
                text = pytesseract.image_to_string(variant, config=cfg)
                compact = re.sub(r"\s+", "", text)
                match = TIME_OCR_RE.search(compact)
                if not match:
                    continue
                hour = int(match.group(1))
                minute = int(match.group(2))
                second = int(match.group(3))
                if 0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60:
                    return hour, minute, second
    return None


def _seconds_of_day(epoch: float) -> float:
    dt = datetime.fromtimestamp(epoch)
    return dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1_000_000


def _match_clock_to_csv(parsed: dict[str, Any], clock: tuple[int, int, int], video_t: float) -> float | None:
    hour, minute, second = clock
    target = hour * 3600 + minute * 60 + second
    times: list[float] = parsed["times"]
    t0 = float(parsed["t0"])
    duration = float(parsed["duration_sec"])

    candidates: list[tuple[float, float, int]] = []
    for idx, epoch in enumerate(times):
        sod = _seconds_of_day(epoch)
        dist = abs(sod - target)
        if dist > 12 * 3600:
            dist = 86400 - dist
        if dist > 2.5:
            continue
        offset = epoch - t0 - video_t
        candidates.append((dist, offset, idx))

    if not candidates:
        return None

    in_range = [item for item in candidates if -15 <= item[1] <= duration + 120]
    pool = in_range if in_range else candidates
    pool.sort(key=lambda item: (item[0], abs(item[1])))
    return float(pool[0][1])


def _detect_video_clock_sync(parsed: dict[str, Any], video_path: Path) -> dict[str, Any]:
    if not video_path.is_file():
        return {
            "mode": "manual",
            "offset_sec": 0.0,
            "ok": False,
            "message": "영상 파일이 없습니다.",
        }
    if not _ffmpeg_bin():
        return {
            "mode": "manual",
            "offset_sec": 0.0,
            "ok": False,
            "message": "ffmpeg를 찾을 수 없습니다.",
        }

    sample_times = [0.5, 1.0, 2.0, 0.0, 3.0, 5.0]
    last_reason = "영상 좌하단 시간 글씨를 인식하지 못했습니다."
    for at_sec in sample_times:
        frame_path = video_path.parent / f"_sync_{uuid.uuid4().hex[:8]}.jpg"
        try:
            if not _extract_video_frame(video_path, at_sec, frame_path):
                last_reason = f"영상 프레임 추출 실패 (t={at_sec:g}s)"
                continue
            clock = _ocr_clock_hms(frame_path)
            if not clock:
                last_reason = f"시간 OCR 실패 (t={at_sec:g}s)"
                continue
            offset = _match_clock_to_csv(parsed, clock, at_sec)
            if offset is None:
                last_reason = (
                    f"CSV에서 {clock[0]:02d}:{clock[1]:02d}:{clock[2]:02d} "
                    f"시각을 찾지 못했습니다."
                )
                continue
            return {
                "mode": "auto",
                "offset_sec": round(offset, 3),
                "ok": True,
                "detected_clock": f"{clock[0]:02d}:{clock[1]:02d}:{clock[2]:02d}",
                "video_t": at_sec,
                "message": (
                    f"영상 {at_sec:g}s 시점의 HUD 시간 "
                    f"{clock[0]:02d}:{clock[1]:02d}:{clock[2]:02d} "
                    f"→ CSV 자동 매칭"
                ),
            }
        finally:
            frame_path.unlink(missing_ok=True)

    return {
        "mode": "manual",
        "offset_sec": 0.0,
        "ok": False,
        "message": last_reason + " 수동 오프셋을 조정하세요.",
    }


def _window_data_decimated(
    decimated: dict[str, Any],
    meta: dict[str, Any],
    *,
    video_t: float,
    offset_sec: float,
    window_sec: float,
    cols: list[str],
) -> dict[str, Any]:
    times: list[float] = decimated["times"]
    t0 = float(meta["t0"])
    center = t0 + offset_sec + max(0.0, video_t)
    half = max(0.5, window_sec)
    start = center - half
    end = center + half

    left, right = 0, len(times)
    while left < right:
        mid = (left + right) // 2
        if times[mid] < start:
            left = mid + 1
        else:
            right = mid
    lo = left
    left, right = 0, len(times)
    while left < right:
        mid = (left + right) // 2
        if times[mid] <= end:
            left = mid + 1
        else:
            right = mid
    hi = left

    if lo >= hi:
        nearest = min(range(len(times)), key=lambda i: abs(times[i] - center))
        lo = max(0, nearest - 1)
        hi = min(len(times), nearest + 2)

    slice_times = times[lo:hi]
    dec_series: dict[str, list[float]] = decimated.get("series") or {}
    series_out: dict[str, list[float | None]] = {}
    for col in cols:
        if col not in dec_series:
            continue
        src = dec_series[col][lo:hi]
        series_out[col] = list(src)

    rel_x = [t - center for t in slice_times]
    return {
        "ok": True,
        "center_abs": center,
        "center_rel": 0.0,
        "video_t": video_t,
        "offset_sec": offset_sec,
        "window_sec": half,
        "x": rel_x,
        "series": series_out,
        "t0": t0,
        "duration_sec": float(meta["duration_sec"]),
        "source": "decimated",
    }


def _window_data(
    meta: dict[str, Any],
    *,
    video_t: float,
    offset_sec: float,
    window_sec: float,
    cols: list[str],
) -> dict[str, Any]:
    decimated = meta.get("decimated")
    if not (isinstance(decimated, dict) and decimated.get("times")):
        loaded = _session_decimated(meta, str(meta.get("id") or ""))
        if isinstance(loaded, dict) and loaded.get("times"):
            meta["decimated"] = loaded
            decimated = loaded
    if isinstance(decimated, dict) and decimated.get("times"):
        return _window_data_decimated(
            decimated,
            meta,
            video_t=video_t,
            offset_sec=offset_sec,
            window_sec=window_sec,
            cols=cols,
        )

    times: list[float] = meta["times"]
    t0 = float(meta["t0"])
    center = t0 + offset_sec + max(0.0, video_t)
    half = max(0.5, window_sec)
    start = center - half
    end = center + half

    lo = 0
    hi = len(times)
    # binary search lower bound
    left, right = 0, len(times)
    while left < right:
        mid = (left + right) // 2
        if times[mid] < start:
            left = mid + 1
        else:
            right = mid
    lo = left
    left, right = 0, len(times)
    while left < right:
        mid = (left + right) // 2
        if times[mid] <= end:
            left = mid + 1
        else:
            right = mid
    hi = left

    if lo >= hi:
        # nearest point fallback
        nearest = min(range(len(times)), key=lambda i: abs(times[i] - center))
        lo = max(0, nearest - 1)
        hi = min(len(times), nearest + 2)

    slice_times = times[lo:hi]
    n = len(slice_times)
    if n > MAX_POINTS:
        step = max(1, n // MAX_POINTS)
        idxs = list(range(0, n, step))
        if idxs[-1] != n - 1:
            idxs.append(n - 1)
    else:
        idxs = list(range(n))

    rel_x = [slice_times[i] - center for i in idxs]
    series_out: dict[str, list[float | None]] = {}
    for col in cols:
        if col not in meta["series"]:
            continue
        src = meta["series"][col][lo:hi]
        series_out[col] = [src[i] for i in idxs]

    return {
        "ok": True,
        "center_abs": center,
        "center_rel": 0.0,
        "video_t": video_t,
        "offset_sec": offset_sec,
        "window_sec": half,
        "x": rel_x,
        "series": series_out,
        "t0": t0,
        "duration_sec": float(meta["duration_sec"]),
        "source": "raw",
    }


def _snapshot_data(
    meta: dict[str, Any],
    *,
    video_t: float,
    offset_sec: float,
    cols: list[str] | None = None,
) -> dict[str, Any]:
    times: list[float] = meta["times"]
    t0 = float(meta["t0"])
    center = t0 + offset_sec + max(0.0, video_t)
    selected = [c for c in (cols or list(VEHICLE_COLS)) if c in meta["series"]]

    left, right = 0, len(times)
    while left < right:
        mid = (left + right) // 2
        if times[mid] < center:
            left = mid + 1
        else:
            right = mid
    idx = min(max(0, left), len(times) - 1)
    if idx > 0 and abs(times[idx - 1] - center) < abs(times[idx] - center):
        idx = idx - 1

    values: dict[str, float | None] = {}
    for col in selected:
        series = meta["series"][col]
        if idx < len(series):
            values[col] = series[idx]

    return {
        "ok": True,
        "video_t": video_t,
        "offset_sec": offset_sec,
        "center_abs": center,
        "index": idx,
        "values": values,
    }


def _run_postprocess(session_id: str) -> None:
    with _lock:
        meta = _sessions.get(session_id)
        if not meta:
            return
        meta["postprocess"] = {
            "status": "running",
            "started_at": datetime.now().timestamp(),
        }

    session_dir = _session_dir(session_id)
    try:
        with _lock:
            meta = _sessions.get(session_id)
            if not meta:
                return
            parsed = {
                "t0": meta["t0"],
                "t_end": meta["t_end"],
                "duration_sec": meta["duration_sec"],
                "row_count": meta["row_count"],
                "times": meta["times"],
                "series": meta["series"],
                "columns": meta["columns"],
            }

        result = run_postprocess(parsed, session_dir)

        with _lock:
            stored = _sessions.get(session_id)
            if not stored:
                return
            stored["series"] = parsed["series"]
            stored["columns"] = parsed["columns"]
            stored["events"] = result["events"]
            stored["kpi"] = result["kpi"]
            stored["segments"] = result["segments"]
            stored["decimated"] = result["decimated"]
            stored["postprocess"] = result["postprocess"]
    except Exception as exc:
        with _lock:
            stored = _sessions.get(session_id)
            if stored:
                stored["postprocess"] = {
                    "status": "error",
                    "message": str(exc),
                    "finished_at": datetime.now().timestamp(),
                }


def _start_postprocess(session_id: str) -> None:
    with _lock:
        meta = _sessions.get(session_id)
        if not meta:
            return
        meta["postprocess"] = {"status": "pending"}
        meta["events"] = []
        meta["kpi"] = None
        meta["segments"] = []
        meta["decimated"] = None
    threading.Thread(target=_run_postprocess, args=(session_id,), daemon=True).start()


def _session_postprocess(meta: dict[str, Any]) -> dict[str, Any]:
    return meta.get("postprocess") or {"status": "unknown"}


def _session_events(meta: dict[str, Any], session_id: str) -> list[dict[str, Any]]:
    events = meta.get("events")
    if isinstance(events, list):
        return events
    return load_events(_session_dir(session_id))


def _session_kpi(meta: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    kpi = meta.get("kpi")
    if isinstance(kpi, dict):
        return kpi
    return load_kpi(_session_dir(session_id))


def _session_segments(meta: dict[str, Any], session_id: str) -> list[dict[str, Any]]:
    segments = meta.get("segments")
    if isinstance(segments, list):
        return segments
    return load_segments(_session_dir(session_id))


def _session_drive_starts(meta: dict[str, Any]) -> list[dict[str, Any]]:
    cached = meta.get("drive_starts")
    if isinstance(cached, list):
        return cached
    times = meta.get("times")
    series = meta.get("series")
    if not isinstance(times, list) or not isinstance(series, dict) or not times:
        return []
    starts = detect_drive_starts(
        {
            "times": times,
            "series": series,
            "t0": meta.get("t0"),
            "raw_timestamps": meta.get("raw_timestamps") or [],
        }
    )
    meta["drive_starts"] = starts
    return starts


def _session_decimated(meta: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    decimated = meta.get("decimated")
    if isinstance(decimated, dict):
        return decimated
    return load_decimated_cache(_session_dir(session_id))



def _restore_session_from_disk(session_id: str) -> dict[str, Any]:
    """Reload a finished upload (CSV required, video optional)."""
    session_id = (session_id or "").strip()
    if not session_id or "/" in session_id or "\\" in session_id:
        raise HTTPException(status_code=400, detail="잘못된 세션 ID")
    dest = _session_dir(session_id)
    csv_path = dest / "data.csv"
    videos = sorted(dest.glob("video.*"))
    if not csv_path.is_file():
        raise HTTPException(status_code=404, detail="디스크에 세션 파일이 없습니다.")

    with _lock:
        existing = _sessions.get(session_id)
        if existing:
            return existing

    has_video = bool(videos)
    video_path = Path("")
    video_ext = ""
    if has_video:
        video_path = videos[0]
        prefer = dest / "video.mp4"
        if prefer.is_file():
            video_path = prefer
        video_ext = video_path.suffix.lower()

    decimated = load_decimated_cache(dest)
    if not isinstance(decimated, dict) or not decimated.get("times"):
        if not has_video:
            parsed = _load_csv(csv_path)
            meta = {
                "id": session_id,
                "created_at": datetime.now().timestamp(),
                "csv_name": "data.csv",
                "video_name": "",
                "video_path": "",
                "video_media_type": "",
                "bytes": csv_path.stat().st_size,
                "t0": parsed["t0"],
                "t_end": parsed["t_end"],
                "duration_sec": parsed["duration_sec"],
                "row_count": parsed["row_count"],
                "columns": parsed["columns"],
                "times": parsed["times"],
                "series": parsed["series"],
                "raw_timestamps": parsed.get("raw_timestamps") or [],
                "decimated": _window_cache_for_parsed(parsed),
                "offset_sec": 0.0,
                "sync": {
                    "mode": "csv_timeline",
                    "offset_sec": 0.0,
                    "ok": True,
                    "message": "CSV-only",
                },
                "playback": {
                    "mode": "csv_timeline",
                    "message": "영상 없음 · CSV timestamp",
                    "ok": True,
                },
                "source": "csv_only",
                "restored": True,
            }
            with _lock:
                _sessions[session_id] = meta
            return meta
        raise HTTPException(
            status_code=409,
            detail="후처리 캐시가 없습니다. 다시 업로드해야 합니다.",
        )

    from postprocess import load_events as _load_events
    from postprocess import load_kpi as _load_kpi
    from postprocess import load_segments as _load_segments

    kpi = _load_kpi(dest)
    events = _load_events(dest)
    segments = _load_segments(dest)

    try:
        pp = json.loads((dest / "postprocess.json").read_text(encoding="utf-8"))
    except Exception:
        pp = {"status": "ready"}

    times = decimated["times"]
    series = decimated.get("series") or {}
    columns = list(series.keys())
    t0 = float(times[0])
    t_end = float(times[-1])
    duration = float((kpi or {}).get("duration_sec") or max(0.0, t_end - t0))
    row_count = int((kpi or {}).get("row_count") or len(times))

    meta = {
        "id": session_id,
        "created_at": datetime.now().timestamp(),
        "csv_name": "data.csv",
        "video_name": video_path.name if has_video else "",
        "video_path": str(video_path) if has_video else "",
        "video_media_type": {
            ".mp4": "video/mp4",
            ".avi": "video/x-msvideo",
            ".webm": "video/webm",
            ".mov": "video/quicktime",
            ".mkv": "video/x-matroska",
        }.get(video_ext, "application/octet-stream") if has_video else "",
        "bytes": csv_path.stat().st_size + (video_path.stat().st_size if has_video else 0),
        "t0": t0,
        "t_end": t_end,
        "duration_sec": duration,
        "row_count": row_count,
        "columns": columns,
        "times": times,
        "series": series,
        "offset_sec": 0.0,
        "sync": {
            "mode": "csv_timeline" if not has_video else "manual",
            "offset_sec": 0.0,
            "ok": (not has_video),
            "message": "CSV-only" if not has_video else "디스크에서 복구됨",
        },
        "playback": {
            "mode": "csv_timeline" if not has_video else "cached",
            "message": "영상 없음 · CSV timestamp" if not has_video else "디스크 세션 복원",
            "ok": True,
        },
        "decimated": decimated,
        "postprocess": pp if isinstance(pp, dict) else {"status": "ready"},
        "kpi": kpi or {},
        "events": events if isinstance(events, list) else [],
        "segments": segments if isinstance(segments, list) else [],
        "source": "csv_only" if not has_video else "restore",
        "restored": True,
    }
    with _lock:
        _sessions[session_id] = meta
    return meta


def _autoload_sessions() -> None:
    if not UPLOAD_DIR.is_dir():
        return
    for path in sorted(UPLOAD_DIR.iterdir()):
        if not path.is_dir():
            continue
        if not (path / "data.csv").is_file():
            continue
        try:
            _restore_session_from_disk(path.name)
            print(f"[analysis] restored session {path.name}")
        except Exception as exc:
            print(f"[analysis] restore skipped {path.name}: {exc}")


@app.on_event("startup")
def _on_startup() -> None:
    _autoload_sessions()


@app.get("/api/sessions")
def list_sessions() -> dict[str, Any]:
    with _lock:
        items = [
            {
                "session_id": sid,
                "csv_name": meta.get("csv_name"),
                "video_name": meta.get("video_name"),
                "row_count": meta.get("row_count"),
                "duration_sec": meta.get("duration_sec"),
                "restored": bool(meta.get("restored")),
                "postprocess": _session_postprocess(meta),
            }
            for sid, meta in _sessions.items()
        ]
    disk = []
    if UPLOAD_DIR.is_dir():
        for path in sorted(UPLOAD_DIR.iterdir()):
            if path.is_dir() and (path / "data.csv").is_file():
                disk.append(path.name)
    return {"ok": True, "sessions": items, "disk_sessions": disk}


@app.post("/api/session/{session_id}/restore")
def restore_session(session_id: str) -> dict[str, Any]:
    meta = _restore_session_from_disk(session_id)
    return _build_session_response(session_id, meta)



@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> Any:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"request": request, **nav_context("analysis", request)},
    )


SESSION_ID_RE = re.compile(r"^\d{8}_\d{6}$")
VIDEO_EXTS = {".mp4", ".avi", ".webm", ".mov", ".mkv"}


def _is_playback_sidecar(path: Path) -> bool:
    """faststart/watch 캐시 — 원본 세션으로 취급하지 않음."""
    name = path.name.lower()
    return (
        name.endswith(".play.mp4")
        or name.endswith(".watch.mp4")
        or path.stem.lower().endswith(".play")
        or path.stem.lower().endswith(".watch")
    )


def _safe_saved_id(session_id: str) -> str:
    session_id = (session_id or "").strip()
    if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id:
        raise HTTPException(status_code=400, detail="잘못된 세션 ID")
    return session_id


def _read_sync_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _resolve_csv_candidates(session_id: str, sync: dict[str, Any] | None = None) -> list[Path]:
    names: list[str] = []
    if sync:
        for key in ("csv_filename", "filename"):
            value = str(sync.get(key) or "").strip()
            if value:
                names.append(Path(value).name)
        prefix = str(sync.get("csv_prefix") or "").strip()
        if prefix:
            names.extend([f"{prefix}.csv", f"{prefix}.csv.gz", f"{prefix}.zip"])
    names.extend(
        [
            f"sensor_log_{session_id}.csv",
            f"sensor_log_{session_id}.csv.gz",
            f"sensor_log_{session_id}.zip",
            f"sensor_log_{session_id}_part001.csv",
            f"sensor_log_{session_id}_part001.csv.gz",
        ]
    )
    found: list[Path] = []
    seen: set[str] = set()
    if not LOG_DIR.is_dir():
        return found
    for name in names:
        path = LOG_DIR / name
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            found.append(path)
    for path in sorted(LOG_DIR.glob(f"sensor_log_{session_id}*")):
        if path.is_file() and str(path) not in seen:
            found.append(path)
    return found


def _resolve_video_candidates(session_id: str, sync: dict[str, Any] | None = None) -> list[Path]:
    names: list[str] = []
    if sync:
        for key in ("camera_filename",):
            value = str(sync.get(key) or "").strip()
            if value:
                names.append(Path(value).name)
        segments = sync.get("camera_segments")
        if isinstance(segments, list):
            for item in segments:
                text = str(item or "").strip()
                if text:
                    names.append(Path(text).name)
        prefix = str(sync.get("camera_prefix") or "").strip()
        if prefix:
            for ext in VIDEO_EXTS:
                names.append(f"{prefix}{ext}")
    for ext in VIDEO_EXTS:
        names.append(f"camera-{session_id}{ext}")
    found: list[Path] = []
    seen: set[str] = set()
    if not CAM_DIR.is_dir():
        return found
    for name in names:
        path = CAM_DIR / name
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file() and path.suffix.lower() in VIDEO_EXTS and not _is_playback_sidecar(path):
            found.append(path)
    for path in sorted(CAM_DIR.glob(f"camera-{session_id}*")):
        if (
            path.is_file()
            and path.suffix.lower() in VIDEO_EXTS
            and not _is_playback_sidecar(path)
            and str(path) not in seen
        ):
            found.append(path)
    return found


def _discover_saved_session_ids() -> list[str]:
    ids: set[str] = set()
    for directory in (CAM_DIR, LOG_DIR):
        if not directory.is_dir():
            continue
        for path in directory.glob("session_sync_*.json"):
            sid = path.stem.replace("session_sync_", "", 1)
            if sid:
                ids.add(sid)
        if directory == LOG_DIR:
            for path in directory.glob("sensor_log_*"):
                stem = path.name
                for suffix in (".csv.gz", ".csv", ".zip"):
                    if stem.endswith(suffix):
                        stem = stem[: -len(suffix)]
                        break
                stem = stem.replace("sensor_log_", "", 1)
                stem = re.sub(r"_part\d+$", "", stem)
                if stem:
                    ids.add(stem)
        else:
            for path in directory.glob("camera-*"):
                if path.suffix.lower() not in VIDEO_EXTS:
                    continue
                if _is_playback_sidecar(path):
                    continue
                stem = path.stem.replace("camera-", "", 1)
                if stem:
                    ids.add(stem)
    return sorted(ids, reverse=True)


def _materialize_csv(src: Path, dest_csv: Path) -> None:
    name = src.name.lower()
    if name.endswith(".csv"):
        shutil.copy2(src, dest_csv)
        return
    if name.endswith(".gz"):
        with gzip.open(src, "rb") as zin, dest_csv.open("wb") as zout:
            shutil.copyfileobj(zin, zout)
        return
    if name.endswith(".zip"):
        with zipfile.ZipFile(src, "r") as zf:
            members = [m for m in zf.namelist() if m.lower().endswith(".csv") and not m.endswith("/")]
            if not members:
                raise HTTPException(status_code=400, detail="ZIP 안에 CSV가 없습니다.")
            with zf.open(members[0]) as zin, dest_csv.open("wb") as zout:
                shutil.copyfileobj(zin, zout)
        return
    raise HTTPException(status_code=400, detail=f"지원하지 않는 CSV 형식: {src.name}")


def _build_session_response(session_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    has_video = bool(str(meta.get("video_path") or "").strip())
    return {
        "ok": True,
        "session_id": session_id,
        "csv_name": meta.get("csv_name"),
        "video_name": meta.get("video_name") or "",
        "columns": meta.get("columns"),
        "row_count": meta.get("row_count"),
        "duration_sec": meta.get("duration_sec"),
        "t0": meta.get("t0"),
        "video_url": f"/api/session/{session_id}/video" if has_video else "",
        "offset_sec": float(meta.get("offset_sec") or 0.0),
        "sync": meta.get("sync"),
        "playback": meta.get("playback"),
        "postprocess": _session_postprocess(meta),
        "source": meta.get("source", "upload"),
        "has_video": has_video,
        "kpi": _session_kpi(meta, session_id),
        "chart_presets": CHART_PRESETS,
        "drive_starts": _session_drive_starts(meta),
    }


def _import_saved_pair(
    *,
    csv_src: Path,
    video_src: Path,
    csv_name: str,
    video_name: str,
    offset_sec: float = 0.0,
    source_label: str = "server",
) -> dict[str, Any]:
    session_id = uuid.uuid4().hex[:12]
    dest = _session_dir(session_id)
    dest.mkdir(parents=True, exist_ok=True)
    csv_path = dest / "data.csv"
    try:
        _materialize_csv(csv_src, csv_path)
        prep = _prepare_playback_video(video_src, dest)
        video_path = Path(prep["path"])
        parsed = _load_csv(csv_path)
    except HTTPException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"가져오기 실패: {exc}") from exc

    total = csv_path.stat().st_size + video_path.stat().st_size
    sync = {
        "mode": "manual",
        "offset_sec": float(offset_sec),
        "ok": False,
        "message": f"서버 저장본에서 가져옴 ({source_label})",
    }
    playback = {
        "mode": prep.get("mode"),
        "message": prep.get("message"),
        "source_bytes": prep.get("source_bytes"),
        "play_bytes": prep.get("play_bytes"),
        "ok": prep.get("ok"),
    }
    meta = {
        "id": session_id,
        "created_at": datetime.now().timestamp(),
        "csv_name": csv_name,
        "video_name": video_name,
        "video_path": str(video_path),
        "video_media_type": prep.get("media_type") or _video_media_type(video_path.suffix),
        "bytes": total,
        "t0": parsed["t0"],
        "t_end": parsed["t_end"],
        "duration_sec": parsed["duration_sec"],
        "row_count": parsed["row_count"],
        "columns": parsed["columns"],
        "times": parsed["times"],
        "series": parsed["series"],
        "raw_timestamps": parsed.get("raw_timestamps") or [],
        "decimated": _window_cache_for_parsed(parsed),
        "offset_sec": float(offset_sec),
        "sync": sync,
        "playback": playback,
        "source": "server_saved",
    }
    with _lock:
        _sessions[session_id] = meta
    _cleanup_old_sessions()
    return _build_session_response(session_id, meta)


@app.get("/api/saved-sessions")
def list_saved_sessions() -> dict[str, Any]:
    """로깅/카메라에 저장된 세션 목록."""
    items: list[dict[str, Any]] = []
    for sid in _discover_saved_session_ids():
        sync = _read_sync_json(CAM_DIR / f"session_sync_{sid}.json") or _read_sync_json(
            LOG_DIR / f"session_sync_{sid}.json"
        )
        csvs = _resolve_csv_candidates(sid, sync)
        videos = _resolve_video_candidates(sid, sync)
        csv_path = csvs[0] if csvs else None
        video_path = videos[0] if videos else None
        if not csv_path and not video_path:
            continue
        mtime = 0.0
        for path in (csv_path, video_path):
            if path and path.is_file():
                mtime = max(mtime, path.stat().st_mtime)
        items.append(
            {
                "saved_id": sid,
                "label": sid,
                "csv": csv_path.name if csv_path else "",
                "video": video_path.name if video_path else "",
                "has_csv": bool(csv_path),
                "has_video": bool(video_path),
                "ready": bool(csv_path),
                "bytes": (csv_path.stat().st_size if csv_path else 0)
                + (video_path.stat().st_size if video_path else 0),
                "mtime": mtime,
                "camera_mode": str((sync or {}).get("camera_mode") or ""),
                "offset_sec_hint": float((sync or {}).get("offset_sec_hint") or 0.0),
            }
        )
    items.sort(key=lambda row: float(row.get("mtime") or 0.0), reverse=True)
    return {
        "ok": True,
        "log_dir": str(LOG_DIR),
        "camera_dir": str(CAM_DIR),
        "sessions": items,
        "count": len(items),
    }


@app.post("/api/import-saved/{saved_id}")
def import_saved_session(saved_id: str) -> dict[str, Any]:
    """서버 저장 CSV(+선택 영상)를 분석 세션으로 가져온다."""
    saved_id = _safe_saved_id(saved_id)
    sync = _read_sync_json(CAM_DIR / f"session_sync_{saved_id}.json") or _read_sync_json(
        LOG_DIR / f"session_sync_{saved_id}.json"
    )
    csvs = _resolve_csv_candidates(saved_id, sync)
    videos = _resolve_video_candidates(saved_id, sync)
    if not csvs:
        raise HTTPException(status_code=404, detail=f"CSV를 찾을 수 없습니다: {saved_id}")
    if videos:
        return _import_saved_pair(
            csv_src=csvs[0],
            video_src=videos[0],
            csv_name=csvs[0].name,
            video_name=videos[0].name,
            offset_sec=float((sync or {}).get("offset_sec_hint") or 0.0),
            source_label=saved_id,
        )
    return _import_csv_only(
        csv_src=csvs[0],
        csv_name=csvs[0].name,
        offset_sec=float((sync or {}).get("offset_sec_hint") or 0.0),
        source_label=saved_id,
    )


def _import_csv_only(
    *,
    csv_src: Path,
    csv_name: str,
    offset_sec: float = 0.0,
    source_label: str = "server",
) -> dict[str, Any]:
    session_id = uuid.uuid4().hex[:12]
    dest = _session_dir(session_id)
    dest.mkdir(parents=True, exist_ok=True)
    csv_path = dest / "data.csv"
    try:
        _materialize_csv(csv_src, csv_path)
        parsed = _load_csv(csv_path)
    except HTTPException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"CSV 가져오기 실패: {exc}") from exc

    sync = {
        "mode": "csv_timeline",
        "offset_sec": float(offset_sec),
        "ok": True,
        "message": f"CSV-only 세션 ({source_label})",
    }
    playback = {
        "mode": "csv_timeline",
        "message": "영상 없음 · CSV timestamp로 재생",
        "ok": True,
    }
    meta = {
        "id": session_id,
        "created_at": datetime.now().timestamp(),
        "csv_name": csv_name,
        "video_name": "",
        "video_path": "",
        "video_media_type": "",
        "bytes": csv_path.stat().st_size,
        "t0": parsed["t0"],
        "t_end": parsed["t_end"],
        "duration_sec": parsed["duration_sec"],
        "row_count": parsed["row_count"],
        "columns": parsed["columns"],
        "times": parsed["times"],
        "series": parsed["series"],
        "raw_timestamps": parsed.get("raw_timestamps") or [],
        "decimated": _window_cache_for_parsed(parsed),
        "offset_sec": float(offset_sec),
        "sync": sync,
        "playback": playback,
        "source": "csv_only",
    }
    with _lock:
        _sessions[session_id] = meta
    _cleanup_old_sessions()
    return _build_session_response(session_id, meta)


@app.post("/api/upload")
async def upload(
    csv_file: UploadFile = File(...),
    video_file: UploadFile | None = File(None),
) -> dict[str, Any]:
    csv_name = Path(csv_file.filename or "data.csv").name
    csv_lower = csv_name.lower()
    if not (csv_lower.endswith(".csv") or csv_lower.endswith(".csv.gz") or csv_lower.endswith(".zip")):
        raise HTTPException(status_code=400, detail="CSV(.csv/.csv.gz/.zip) 파일만 업로드할 수 있습니다.")

    has_video = bool(video_file is not None and (video_file.filename or "").strip())
    video_name = Path(video_file.filename or "video.mp4").name if has_video else ""
    video_ext = Path(video_name).suffix.lower() if has_video else ""
    if has_video and video_ext not in {".mp4", ".avi", ".webm", ".mov", ".mkv"}:
        raise HTTPException(status_code=400, detail="지원 영상 형식: mp4, avi, webm, mov, mkv")

    session_id = uuid.uuid4().hex[:12]
    dest = _session_dir(session_id)
    dest.mkdir(parents=True, exist_ok=True)

    csv_path = dest / "data.csv"
    total = 0
    try:
        raw_csv = dest / f"_upload_csv{Path(csv_name).suffix.lower() or '.csv'}"
        with raw_csv.open("wb") as out:
            while True:
                chunk = await csv_file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_SESSION_BYTES:
                    raise HTTPException(status_code=413, detail="업로드 용량 한도 초과")
                out.write(chunk)
        _materialize_csv(raw_csv, csv_path)
        raw_csv.unlink(missing_ok=True)

        video_path = Path("")
        prep: dict[str, Any] = {}
        if has_video and video_file is not None:
            raw_video_path = dest / f"_upload{video_ext}"
            with raw_video_path.open("wb") as out:
                while True:
                    chunk = await video_file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_SESSION_BYTES:
                        raise HTTPException(status_code=413, detail="업로드 용량 한도 초과")
                    out.write(chunk)
            prep = _prepare_playback_video(raw_video_path, dest)
            video_path = Path(prep["path"])
            raw_video_path.unlink(missing_ok=True)

        parsed = _load_csv(csv_path)
    except HTTPException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"CSV 파싱 실패: {exc}") from exc

    if has_video:
        sync = {
            "mode": "manual",
            "offset_sec": 0.0,
            "ok": False,
            "message": "manual offset sync check",
        }
        playback = {
            "mode": prep.get("mode"),
            "message": prep.get("message"),
            "source_bytes": prep.get("source_bytes"),
            "play_bytes": prep.get("play_bytes"),
            "ok": prep.get("ok"),
        }
        bytes_total = csv_path.stat().st_size + video_path.stat().st_size
    else:
        sync = {
            "mode": "csv_timeline",
            "offset_sec": 0.0,
            "ok": True,
            "message": "CSV-only · timestamp 타임라인",
        }
        playback = {
            "mode": "csv_timeline",
            "message": "영상 없음 · CSV timestamp로 재생/스크럽",
            "ok": True,
        }
        bytes_total = csv_path.stat().st_size
        video_name = ""

    meta = {
        "id": session_id,
        "created_at": datetime.now().timestamp(),
        "csv_name": csv_name,
        "video_name": video_name,
        "video_path": str(video_path) if has_video else "",
        "video_media_type": (prep.get("media_type") or _video_media_type(video_path.suffix)) if has_video else "",
        "bytes": bytes_total,
        "t0": parsed["t0"],
        "t_end": parsed["t_end"],
        "duration_sec": parsed["duration_sec"],
        "row_count": parsed["row_count"],
        "columns": parsed["columns"],
        "times": parsed["times"],
        "series": parsed["series"],
        "raw_timestamps": parsed.get("raw_timestamps") or [],
        "decimated": _window_cache_for_parsed(parsed),
        "offset_sec": float(sync.get("offset_sec") or 0.0),
        "sync": sync,
        "playback": playback,
        "source": "upload" if has_video else "csv_only",
    }

    with _lock:
        _sessions[session_id] = meta
    _cleanup_old_sessions()
    pass  # postprocess disabled for sync-check UI

    return _build_session_response(session_id, meta)


@app.get("/api/session/{session_id}")
def session_info(session_id: str) -> dict[str, Any]:
    with _lock:
        meta = _sessions.get(session_id)
    if not meta:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    resp = _build_session_response(session_id, meta)
    resp["event_count"] = len(_session_events(meta, session_id))
    resp["segment_count"] = len(_session_segments(meta, session_id))
    resp["vehicle"] = vehicle_public_spec()
    return resp


@app.get("/api/session/{session_id}/columns")
def session_columns(session_id: str) -> dict[str, Any]:
    with _lock:
        meta = _sessions.get(session_id)
    if not meta:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    return {"ok": True, "columns": meta["columns"]}


@app.post("/api/session/{session_id}/autosync")
def session_autosync(session_id: str) -> dict[str, Any]:
    with _lock:
        meta = _sessions.get(session_id)
    if not meta:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")

    parsed = {
        "t0": meta["t0"],
        "duration_sec": meta["duration_sec"],
        "times": meta["times"],
    }
    video_path = Path(meta["video_path"])
    sync = _detect_video_clock_sync(parsed, video_path)

    with _lock:
        stored = _sessions.get(session_id)
        if stored:
            stored["sync"] = sync
            stored["offset_sec"] = float(sync.get("offset_sec") or 0.0)

    return {
        "ok": True,
        "session_id": session_id,
        "offset_sec": float(sync.get("offset_sec") or 0.0),
        "sync": sync,
    }


@app.get("/api/session/{session_id}/snapshot")
def session_snapshot(
    session_id: str,
    t: float = 0.0,
    offset: float = 0.0,
    cols: str = "",
) -> dict[str, Any]:
    with _lock:
        meta = _sessions.get(session_id)
    if not meta:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")

    selected = [c.strip() for c in cols.split(",") if c.strip()]
    if not selected:
        selected = [c for c in VEHICLE_COLS if c in meta["columns"]]

    return _snapshot_data(
        meta,
        video_t=float(t),
        offset_sec=float(offset),
        cols=selected,
    )


@app.get("/api/session/{session_id}/window")
def session_window(
    session_id: str,
    t: float = 0.0,
    offset: float = 0.0,
    window: float = DEFAULT_WINDOW_SEC,
    cols: str = "",
) -> dict[str, Any]:
    with _lock:
        meta = _sessions.get(session_id)
    if not meta:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")

    selected = [c.strip() for c in cols.split(",") if c.strip()]
    if not selected:
        selected = meta["columns"][:4]
    selected = [c for c in selected if c in meta["columns"]]
    if not selected:
        raise HTTPException(status_code=400, detail="선택한 컬럼이 없습니다.")

    return _window_data(
        meta,
        video_t=float(t),
        offset_sec=float(offset),
        window_sec=float(window or DEFAULT_WINDOW_SEC),
        cols=selected,
    )


@app.get("/api/session/{session_id}/events")
def session_events(
    session_id: str,
    type: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    with _lock:
        meta = _sessions.get(session_id)
    if not meta:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")

    events = _session_events(meta, session_id)
    if type:
        events = [evt for evt in events if evt.get("type") == type]
    limit = max(1, min(int(limit or 200), 1000))
    return {
        "ok": True,
        "session_id": session_id,
        "postprocess": _session_postprocess(meta),
        "count": len(events),
        "events": events[:limit],
    }


@app.get("/api/session/{session_id}/drive-starts")
def session_drive_starts(session_id: str) -> dict[str, Any]:
    with _lock:
        meta = _sessions.get(session_id)
    if not meta:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    # 이전 빈 캐시가 남아 있으면 다시 계산
    meta.pop("drive_starts", None)
    starts = _session_drive_starts(meta)
    return {
        "ok": True,
        "session_id": session_id,
        "count": len(starts),
        "drive_starts": starts,
    }


@app.get("/api/session/{session_id}/kpi")
def session_kpi(session_id: str) -> dict[str, Any]:
    with _lock:
        meta = _sessions.get(session_id)
    if not meta:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")

    kpi = _session_kpi(meta, session_id)
    if not kpi:
        return {
            "ok": True,
            "session_id": session_id,
            "postprocess": _session_postprocess(meta),
            "kpi": None,
        }
    return {
        "ok": True,
        "session_id": session_id,
        "postprocess": _session_postprocess(meta),
        "kpi": kpi,
    }


@app.get("/api/session/{session_id}/segments")
def session_segments(
    session_id: str,
    state: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    with _lock:
        meta = _sessions.get(session_id)
    if not meta:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")

    segments = _session_segments(meta, session_id)
    if state:
        segments = [seg for seg in segments if seg.get("state") == state]
    limit = max(1, min(int(limit or 200), 1000))
    return {
        "ok": True,
        "session_id": session_id,
        "postprocess": _session_postprocess(meta),
        "count": len(segments),
        "segments": segments[:limit],
    }


@app.get("/api/session/{session_id}/compare")
def session_compare(
    session_id: str,
    a: str = "",
    b: str = "",
    cols: str = "",
    points: int = 160,
) -> dict[str, Any]:
    if not a or not b:
        raise HTTPException(status_code=400, detail="비교할 구간 id a, b가 필요합니다.")
    with _lock:
        meta = _sessions.get(session_id)
    if not meta:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")

    segments = _session_segments(meta, session_id)
    seg_a = find_segment(segments, a)
    seg_b = find_segment(segments, b)
    if not seg_a or not seg_b:
        raise HTTPException(status_code=404, detail="구간을 찾을 수 없습니다.")

    selected = [c.strip() for c in cols.split(",") if c.strip()]
    if not selected:
        selected = [c for c in ("speed", "steering_angle", "lat_g", "break_p") if c in meta["columns"]]

    parsed = {
        "times": meta["times"],
        "series": meta["series"],
    }
    payload = compare_segments(parsed, seg_a, seg_b, selected, points=points)
    return {
        "ok": True,
        "session_id": session_id,
        **payload,
    }


@app.get("/api/vehicle")
def vehicle_info() -> dict[str, Any]:
    spec = vehicle_public_spec()
    return {
        "ok": True,
        "vehicle": spec,
    }


@app.get("/api/chart-presets")
def chart_presets() -> dict[str, Any]:
    return {"ok": True, "presets": CHART_PRESETS}


@app.get("/api/session/{session_id}/video")
def session_video(session_id: str) -> FileResponse:
    with _lock:
        meta = _sessions.get(session_id)
    if not meta:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    path = Path(meta["video_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="영상 파일이 없습니다.")
    return FileResponse(
        path=path,
        media_type=meta.get("video_media_type") or "video/mp4",
        filename=meta.get("video_name") or path.name,
        headers={"Accept-Ranges": "bytes", "Cache-Control": "no-store"},
    )


@app.delete("/api/session/{session_id}")
def delete_session(session_id: str) -> dict[str, Any]:
    with _lock:
        meta = _sessions.pop(session_id, None)
    path = _session_dir(session_id)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    if not meta:
        return {"ok": False, "message": "세션 없음"}
    return {"ok": True, "deleted": session_id}


@app.get("/api/health")
def health() -> dict[str, Any]:
    with _lock:
        count = len(_sessions)
    return {
        "ok": True,
        "sessions": count,
        "upload_dir": str(UPLOAD_DIR),
        "max_session_bytes": MAX_SESSION_BYTES,
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", os.getenv("ANALYSIS_PORT", "8013")))
    uvicorn.run("analysis:app", host="0.0.0.0", port=port, reload=False)
