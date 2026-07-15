"""CSV post-processing: derived channels, events, KPI."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from signal_util import (
    decimate_bucket_average,
    gradient,
    local_maxima,
    moving_average,
    num,
    percentile,
    resample_values,
)

from vehicle_spec import load_vehicle_spec

POSTPROCESS_VERSION = 3
DECIMATED_TARGET_HZ = 20.0

CHART_PRESETS: dict[str, list[str]] = {
    "chassis": ["speed", "steering_angle", "pitch_deg", "roll_deg"],
    "suspension": ["linear_fl", "linear_fr", "linear_rl", "linear_rr", "sus_roll_deg"],
    "gforce": ["lat_g", "long_g", "total_g"],
    "power": ["INV_torque_feedback", "INV_dc_currnet", "BMS_current", "BMS_voltage"],
    "driver": ["accel_p", "break_p", "steering_angle", "speed"],
}

DERIVED_COLUMNS = (
    "pitch_deg",
    "roll_deg",
    "lat_g",
    "long_g",
    "total_g",
    "linear_avg",
    "linear_fl_dev",
    "linear_fr_dev",
    "linear_rl_dev",
    "linear_rr_dev",
    "sus_pitch_deg",
    "sus_roll_deg",
    "steer_rate",
    "yaw_rate_est",
)


def _series_or_empty(series: dict[str, list[float | None]], key: str, n: int) -> list[float | None]:
    if key in series and len(series[key]) == n:
        return series[key]
    return [0.0] * n


def build_derived(parsed: dict[str, Any], spec: dict[str, Any] | None = None) -> dict[str, list[float]]:
    vehicle = spec or load_vehicle_spec()
    wheelbase = float(vehicle.get("wheelbase_m") or 2.4)
    track = float(vehicle.get("track_m") or 1.68)
    sus_gain = float((vehicle.get("derived") or {}).get("sus_angle_gain") or 1.0)
    use_yaw = bool((vehicle.get("derived") or {}).get("yaw_from_steer", True))
    times: list[float] = parsed["times"]
    series: dict[str, list[float | None]] = parsed["series"]
    n = len(times)
    if n == 0:
        return {}

    dt = (times[-1] - times[0]) / max(1, n - 1)
    if dt <= 0:
        dt = 0.005

    ax = _series_or_empty(series, "accelX", n)
    ay = _series_or_empty(series, "accelY", n)
    az = _series_or_empty(series, "accelZ", n)
    steer = _series_or_empty(series, "steering_angle", n)
    lin_fl = _series_or_empty(series, "linear_fl", n)
    lin_fr = _series_or_empty(series, "linear_fr", n)
    lin_rl = _series_or_empty(series, "linear_rl", n)
    lin_rr = _series_or_empty(series, "linear_rr", n)

    pitch_raw = [
        math.degrees(math.atan2(-num(x), max(0.15, abs(num(z, 1.0))))) for x, z in zip(ax, az)
    ]
    roll_raw = [
        math.degrees(math.atan2(num(y), max(0.15, abs(num(z, 1.0))))) for y, z in zip(ay, az)
    ]
    pitch_deg = moving_average(pitch_raw, window=20)
    roll_deg = moving_average(roll_raw, window=20)

    lat_g = moving_average([num(y) for y in ay], window=10)
    long_g = moving_average([num(x) for x in ax], window=10)
    total_g = [
        math.sqrt(num(x) ** 2 + num(y) ** 2 + num(z) ** 2) for x, y, z in zip(ax, ay, az)
    ]

    linear_avg = [
        (num(fl) + num(fr) + num(rl) + num(rr)) / 4.0
        for fl, fr, rl, rr in zip(lin_fl, lin_fr, lin_rl, lin_rr)
    ]
    fl_dev = [avg - num(fl) for avg, fl in zip(linear_avg, lin_fl)]
    fr_dev = [avg - num(fr) for avg, fr in zip(linear_avg, lin_fr)]
    rl_dev = [avg - num(rl) for avg, rl in zip(linear_avg, lin_rl)]
    rr_dev = [avg - num(rr) for avg, rr in zip(linear_avg, lin_rr)]

    sus_pitch: list[float] = []
    sus_roll: list[float] = []
    for fl, fr, rl, rr in zip(fl_dev, fr_dev, rl_dev, rr_dev):
        front = (fl + fr) * 0.5
        rear = (rl + rr) * 0.5
        left = (fl + rl) * 0.5
        right = (fr + rr) * 0.5
        sus_pitch.append(math.degrees(math.atan2(rear - front, wheelbase)) * sus_gain)
        sus_roll.append(math.degrees(math.atan2(right - left, track)) * sus_gain)

    sus_pitch = moving_average(sus_pitch, window=15)
    sus_roll = moving_average(sus_roll, window=15)
    steer_rate = gradient(steer, dt)

    yaw_rate_est: list[float] = []
    if use_yaw:
        speed = [num(v) for v in _series_or_empty(series, "speed", n)]
        for spd_kmh, ang in zip(speed, steer):
            spd_ms = max(0.0, spd_kmh) / 3.6
            steer_rad = math.radians(num(ang))
            if spd_ms < 1.0 or wheelbase <= 0:
                yaw_rate_est.append(0.0)
            else:
                yaw_rate_est.append(math.degrees(math.atan2(spd_ms * math.tan(steer_rad), wheelbase)))
    else:
        yaw_rate_est = [0.0] * n

    return {
        "pitch_deg": pitch_deg,
        "roll_deg": roll_deg,
        "lat_g": lat_g,
        "long_g": long_g,
        "total_g": total_g,
        "linear_avg": linear_avg,
        "linear_fl_dev": fl_dev,
        "linear_fr_dev": fr_dev,
        "linear_rl_dev": rl_dev,
        "linear_rr_dev": rr_dev,
        "sus_pitch_deg": sus_pitch,
        "sus_roll_deg": sus_roll,
        "steer_rate": steer_rate,
        "yaw_rate_est": yaw_rate_est,
    }


def detect_events(parsed: dict[str, Any], derived: dict[str, list[float]]) -> list[dict[str, Any]]:
    times: list[float] = parsed["times"]
    series: dict[str, list[float | None]] = parsed["series"]
    t0 = float(parsed["t0"])
    n = len(times)
    if n < 3:
        return []

    dt = (times[-1] - times[0]) / max(1, n - 1)
    if dt <= 0:
        dt = 0.005
    min_spacing = max(20, int(2.0 / dt))

    speed = [num(v) for v in _series_or_empty(series, "speed", n)]
    brake = [num(v) for v in _series_or_empty(series, "break_p", n)]
    steer = [num(v) for v in _series_or_empty(series, "steering_angle", n)]
    lat_g = derived.get("lat_g") or [0.0] * n

    events: list[dict[str, Any]] = []
    seq = 0

    def add_event(
        event_type: str,
        idx: int,
        *,
        label: str,
        value: float,
        extra: dict[str, Any] | None = None,
    ) -> None:
        nonlocal seq
        seq += 1
        t_abs = float(times[idx])
        payload: dict[str, Any] = {
            "id": f"evt_{seq:04d}",
            "type": event_type,
            "t_abs": round(t_abs, 3),
            "t_rel": round(t_abs - t0, 3),
            "index": idx,
            "label": label,
            "value": round(float(value), 3),
        }
        if extra:
            payload.update(extra)
        events.append(payload)

    brake_abs = [abs(v) for v in brake]
    for idx in local_maxima(brake_abs, min_value=1.0, min_spacing=min_spacing):
        if speed[idx] < 25:
            continue
        add_event(
            "brake_peak",
            idx,
            label=f"브레이크 {brake[idx]:.1f}",
            value=brake[idx],
            extra={"speed": round(speed[idx], 1), "break_p": round(brake[idx], 2)},
        )

    steer_abs = [abs(v) for v in steer]
    for idx in local_maxima(steer_abs, min_value=15.0, min_spacing=min_spacing):
        if speed[idx] < 15:
            continue
        add_event(
            "steer_peak",
            idx,
            label=f"조향 {steer[idx]:+.0f}°",
            value=steer[idx],
            extra={"speed": round(speed[idx], 1), "steering_angle": round(steer[idx], 1)},
        )

    lat_abs = [abs(v) for v in lat_g]
    for idx in local_maxima(lat_abs, min_value=0.25, min_spacing=min_spacing):
        if speed[idx] < 20:
            continue
        add_event(
            "max_lat_g",
            idx,
            label=f"횡G {lat_g[idx]:+.2f}g",
            value=lat_g[idx],
            extra={"speed": round(speed[idx], 1), "lat_g": round(lat_g[idx], 3)},
        )

    for idx in range(1, n):
        if speed[idx - 1] < 5 and speed[idx] >= 5:
            add_event(
                "drive_start",
                idx,
                label="주행 시작",
                value=speed[idx],
                extra={"speed": round(speed[idx], 1)},
            )
            break

    events.sort(key=lambda item: item["t_abs"])
    for i, evt in enumerate(events, start=1):
        evt["id"] = f"evt_{i:04d}"
    return events


def _classify_drive_state(
    *,
    speed: float,
    brake: float,
    steer: float,
    lat_g: float,
    throttle: float,
    long_g: float,
    fsm: dict[str, Any],
) -> str:
    stop_speed = float(fsm.get("stop_speed_kmh") or 5)
    brake_min = float(fsm.get("brake_pressure_min") or 0.5)
    long_brake = float(fsm.get("long_brake_g") or -0.18)
    corner_speed = float(fsm.get("corner_min_speed_kmh") or 15)
    corner_steer = float(fsm.get("corner_min_steer_deg") or 8)
    corner_lat = float(fsm.get("corner_min_lat_g") or 0.05)
    corner_steer_alt = float(fsm.get("corner_steer_alt_deg") or 15)
    accel_thr = float(fsm.get("accel_throttle_min") or 3)
    accel_g = float(fsm.get("accel_long_g") or 0.12)

    if speed < stop_speed:
        return "stop"
    if brake > brake_min or long_g < long_brake:
        return "brake"
    if speed > corner_speed and abs(steer) > corner_steer and (
        abs(lat_g) > corner_lat or abs(steer) > corner_steer_alt
    ):
        return "corner"
    if throttle > accel_thr or long_g > accel_g:
        return "accel"
    return "cruise"


def _segment_stats(
    parsed: dict[str, Any],
    derived: dict[str, list[float]],
    start: int,
    end: int,
) -> dict[str, float]:
    series = parsed["series"]
    n = end - start + 1
    if n <= 0:
        return {}

    def slice_num(key: str, default: float = 0.0) -> list[float]:
        if key in derived:
            return derived[key][start : end + 1]
        if key in series:
            return [num(v, default) for v in series[key][start : end + 1]]
        return []

    speed = slice_num("speed")
    steer = slice_num("steering_angle")
    lat = slice_num("lat_g")
    brake = slice_num("break_p")

    return {
        "speed_min": round(min(speed) if speed else 0.0, 1),
        "speed_max": round(max(speed) if speed else 0.0, 1),
        "speed_avg": round(sum(speed) / len(speed), 1) if speed else 0.0,
        "steer_peak": round(max((abs(v) for v in steer), default=0.0), 1),
        "lat_g_peak": round(max((abs(v) for v in lat), default=0.0), 3),
        "brake_max": round(max(brake) if brake else 0.0, 2),
    }


def detect_segments(
    parsed: dict[str, Any],
    derived: dict[str, list[float]],
    spec: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    vehicle = spec or load_vehicle_spec()
    fsm = vehicle.get("fsm") or {}
    min_segment_sec = float(fsm.get("min_segment_sec") or 0.5)
    times: list[float] = parsed["times"]
    series: dict[str, list[float | None]] = parsed["series"]
    t0 = float(parsed["t0"])
    n = len(times)
    if n < 3:
        return []

    dt = (times[-1] - times[0]) / max(1, n - 1)
    if dt <= 0:
        dt = 0.005
    min_samples = max(3, int(min_segment_sec / dt))

    speed = [num(v) for v in _series_or_empty(series, "speed", n)]
    brake = [num(v) for v in _series_or_empty(series, "break_p", n)]
    steer = [num(v) for v in _series_or_empty(series, "steering_angle", n)]
    throttle = [num(v) for v in _series_or_empty(series, "accel_p", n)]
    lat_g = derived.get("lat_g") or [0.0] * n
    long_g = derived.get("long_g") or [0.0] * n

    states: list[str] = []
    for i in range(n):
        states.append(
            _classify_drive_state(
                speed=speed[i],
                brake=brake[i],
                steer=steer[i],
                lat_g=lat_g[i],
                throttle=throttle[i],
                long_g=long_g[i],
                fsm=fsm,
            )
        )

    raw_segments: list[tuple[str, int, int]] = []
    cur_state = states[0]
    start = 0
    for i in range(1, n):
        if states[i] != cur_state:
            raw_segments.append((cur_state, start, i - 1))
            cur_state = states[i]
            start = i
    raw_segments.append((cur_state, start, n - 1))

    segments: list[dict[str, Any]] = []
    corner_no = 0
    seg_no = 0
    for state, s_idx, e_idx in raw_segments:
        if e_idx - s_idx + 1 < min_samples and state != "corner":
            continue
        if state == "corner" and e_idx - s_idx + 1 < min_samples:
            continue

        seg_no += 1
        t_start = float(times[s_idx])
        t_end = float(times[e_idx])
        duration = max(0.0, t_end - t_start)
        stats = _segment_stats(parsed, derived, s_idx, e_idx)

        if state == "corner":
            corner_no += 1
            label = f"코너 {corner_no}"
        elif state == "brake":
            label = f"제동 {seg_no}"
        elif state == "accel":
            label = f"가속 {seg_no}"
        elif state == "cruise":
            label = f"주행 {seg_no}"
        else:
            label = f"정지 {seg_no}"

        segments.append(
            {
                "id": f"seg_{seg_no:03d}",
                "state": state,
                "start_idx": s_idx,
                "end_idx": e_idx,
                "t_start_abs": round(t_start, 3),
                "t_end_abs": round(t_end, 3),
                "t_start_rel": round(t_start - t0, 3),
                "t_end_rel": round(t_end - t0, 3),
                "duration_sec": round(duration, 2),
                "label": label,
                "stats": stats,
            }
        )
    return segments


def build_decimated_cache(parsed: dict[str, Any], *, target_hz: float = DECIMATED_TARGET_HZ) -> dict[str, Any]:
    times = parsed["times"]
    series = parsed["series"]
    d_times, d_series = decimate_bucket_average(times, series, target_hz=target_hz)
    return {
        "target_hz": target_hz,
        "t0": float(parsed["t0"]),
        "count": len(d_times),
        "times": d_times,
        "series": d_series,
    }


def compare_segments(
    parsed: dict[str, Any],
    seg_a: dict[str, Any],
    seg_b: dict[str, Any],
    cols: list[str],
    *,
    points: int = 160,
) -> dict[str, Any]:
    times = parsed["times"]
    series = parsed["series"]
    points = max(20, min(int(points or 160), 400))
    x_pct = [round(i * 100.0 / (points - 1), 2) for i in range(points)]

    out_series: dict[str, dict[str, list[float]]] = {}
    for col in cols:
        if col not in series:
            continue
        vals = series[col]
        a_slice = vals[seg_a["start_idx"] : seg_a["end_idx"] + 1]
        b_slice = vals[seg_b["start_idx"] : seg_b["end_idx"] + 1]
        out_series[col] = {
            "a": [round(v, 4) for v in resample_values(a_slice, points)],
            "b": [round(v, 4) for v in resample_values(b_slice, points)],
        }

    return {
        "x_pct": x_pct,
        "points": points,
        "segment_a": {"id": seg_a["id"], "label": seg_a["label"], "state": seg_a["state"]},
        "segment_b": {"id": seg_b["id"], "label": seg_b["label"], "state": seg_b["state"]},
        "series": out_series,
    }


def compute_kpi(
    parsed: dict[str, Any],
    derived: dict[str, list[float]],
    events: list[dict[str, Any]],
    segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    times: list[float] = parsed["times"]
    series: dict[str, list[float | None]] = parsed["series"]
    n = len(times)
    speed = [num(v) for v in _series_or_empty(series, "speed", n)]
    lat_g = derived.get("lat_g") or [0.0] * n
    long_g = derived.get("long_g") or [0.0] * n
    brake = [num(v) for v in _series_or_empty(series, "break_p", n)]

    dt = (times[-1] - times[0]) / max(1, n - 1) if n > 1 else 0.005
    distance_m = sum((speed[i] / 3.6) * dt for i in range(1, n))

    event_counts: dict[str, int] = {}
    for evt in events:
        event_counts[evt["type"]] = event_counts.get(evt["type"], 0) + 1

    moving = [s for s in speed if s > 5]
    corners = [s for s in (segments or []) if s.get("state") == "corner"]
    brakes = [s for s in (segments or []) if s.get("state") == "brake"]

    inv_torque = [num(v) for v in _series_or_empty(series, "INV_torque_feedback", n)]
    bms_current = [num(v) for v in _series_or_empty(series, "BMS_current", n)]
    bms_voltage = [num(v) for v in _series_or_empty(series, "BMS_voltage", n)]
    inv_power = [
        num(v) * num(c) / 1000.0
        for v, c in zip(bms_voltage, bms_current)
        if num(v) > 0 or num(c) != 0
    ]
    if not inv_power:
        inv_power = [
            num(v) * num(c) / 1000.0
            for v, c in zip(_series_or_empty(series, "INV_voltage", n), _series_or_empty(series, "INV_dc_currnet", n))
        ]

    steer_vals = [num(v) for v in _series_or_empty(series, "steering_angle", n)]
    return {
        "duration_sec": round(float(parsed["duration_sec"]), 2),
        "row_count": int(parsed["row_count"]),
        "sample_hz": round(1.0 / dt, 1) if dt > 0 else 0.0,
        "distance_km": round(distance_m / 1000.0, 2),
        "speed": {
            "max": round(max(speed) if speed else 0.0, 1),
            "avg": round(sum(moving) / len(moving), 1) if moving else 0.0,
            "p95": round(percentile(moving, 95), 1) if moving else 0.0,
        },
        "g": {
            "lat_max": round(max((abs(v) for v in lat_g), default=0.0), 3),
            "long_brake_min": round(min(long_g) if long_g else 0.0, 3),
            "total_max": round(max(derived.get("total_g") or [0.0]), 3),
        },
        "brake": {
            "max": round(max(brake) if brake else 0.0, 2),
        },
        "attitude": {
            "pitch_max_deg": round(
                max((abs(v) for v in derived.get("pitch_deg") or [0.0]), default=0.0), 2
            ),
            "roll_max_deg": round(
                max((abs(v) for v in derived.get("roll_deg") or [0.0]), default=0.0), 2
            ),
        },
        "steering": {
            "max_abs_deg": round(max((abs(v) for v in steer_vals), default=0.0), 1),
        },
        "segments": {
            "total": len(segments or []),
            "corners": len(corners),
            "brakes": len(brakes),
            "avg_corner_sec": round(
                sum(float(s.get("duration_sec") or 0) for s in corners) / len(corners), 2
            )
            if corners
            else 0.0,
        },
        "powertrain": {
            "peak_power_kw": round(max(inv_power) if inv_power else 0.0, 1),
            "avg_power_kw": round(sum(inv_power) / len(inv_power), 1) if inv_power else 0.0,
            "torque_peak": round(max((abs(v) for v in inv_torque), default=0.0), 1),
        },
        "events": event_counts,
        "event_total": len(events),
        "chart_presets": CHART_PRESETS,
    }


def merge_derived(parsed: dict[str, Any], derived: dict[str, list[float]]) -> None:
    series = parsed["series"]
    columns = list(parsed["columns"])
    for col, values in derived.items():
        series[col] = values
        if col not in columns:
            columns.append(col)
    parsed["columns"] = columns


def save_artifacts(
    session_dir: Path,
    events: list[dict[str, Any]],
    kpi: dict[str, Any],
    postprocess: dict[str, Any],
    segments: list[dict[str, Any]],
    decimated: dict[str, Any] | None = None,
) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "events.json").write_text(
        json.dumps({"events": events}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (session_dir / "kpi.json").write_text(
        json.dumps(kpi, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (session_dir / "segments.json").write_text(
        json.dumps({"segments": segments}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (session_dir / "postprocess.json").write_text(
        json.dumps(postprocess, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if decimated:
        cache_dir = session_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "decimated.json").write_text(
            json.dumps(decimated, ensure_ascii=False),
            encoding="utf-8",
        )


def load_events(session_dir: Path) -> list[dict[str, Any]]:
    path = session_dir / "events.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data.get("events") or [])
    except (json.JSONDecodeError, OSError):
        return []


def load_kpi(session_dir: Path) -> dict[str, Any] | None:
    path = session_dir / "kpi.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_segments(session_dir: Path) -> list[dict[str, Any]]:
    path = session_dir / "segments.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data.get("segments") or [])
    except (json.JSONDecodeError, OSError):
        return []


def load_decimated_cache(session_dir: Path) -> dict[str, Any] | None:
    path = session_dir / "cache" / "decimated.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def find_segment(segments: list[dict[str, Any]], segment_id: str) -> dict[str, Any] | None:
    for seg in segments:
        if seg.get("id") == segment_id:
            return seg
    return None


def run(parsed: dict[str, Any], session_dir: Path) -> dict[str, Any]:
    started = time.time()
    spec = load_vehicle_spec()
    derived = build_derived(parsed, spec)
    events = detect_events(parsed, derived)
    segments = detect_segments(parsed, derived, spec)
    kpi = compute_kpi(parsed, derived, events, segments)
    kpi["vehicle"] = {
        "name": spec.get("name"),
        "wheelbase_m": spec.get("wheelbase_m"),
        "track_m": spec.get("track_m"),
    }
    decimated = build_decimated_cache(parsed)
    merge_derived(parsed, derived)

    finished = time.time()
    postprocess = {
        "status": "ready",
        "version": POSTPROCESS_VERSION,
        "started_at": started,
        "finished_at": finished,
        "elapsed_sec": round(finished - started, 3),
        "derived_columns": list(DERIVED_COLUMNS),
        "event_count": len(events),
        "segment_count": len(segments),
        "corner_count": len([s for s in segments if s.get("state") == "corner"]),
        "decimated_hz": decimated.get("target_hz"),
        "decimated_points": decimated.get("count"),
        "vehicle": spec.get("name"),
    }
    save_artifacts(session_dir, events, kpi, postprocess, segments, decimated)
    return {
        "derived": derived,
        "events": events,
        "segments": segments,
        "kpi": kpi,
        "decimated": decimated,
        "postprocess": postprocess,
        "vehicle_spec": spec,
    }
