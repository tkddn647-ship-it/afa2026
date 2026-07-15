"""Server-side vehicle specification loader."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SPEC_PATH = BASE_DIR / "vehicle.json"

_DEFAULTS: dict[str, Any] = {
    "name": "A-FA",
    "wheelbase_m": 2.4,
    "track_m": 1.68,
    "tire_radius_m": 0.33,
    "steer_ratio": 12.0,
    "mass_kg": 650,
    "linear_sensor": {
        "neutral_mm": 40.0,
        "mm_per_count": 1.0,
        "visual_scale": 0.022,
    },
    "derived": {
        "sus_angle_gain": 1.0,
        "yaw_from_steer": True,
    },
    "visualization": {
        "pitch_gain": 0.22,
        "roll_gain": 0.35,
        "tilt_max_deg": 10.0,
        "sus_gain": 0.85,
        "sus_tilt_max_deg": 6.0,
    },
    "fsm": {
        "stop_speed_kmh": 5,
        "brake_pressure_min": 0.5,
        "long_brake_g": -0.18,
        "corner_min_speed_kmh": 15,
        "corner_min_steer_deg": 8,
        "corner_min_lat_g": 0.05,
        "corner_steer_alt_deg": 15,
        "accel_throttle_min": 3,
        "accel_long_g": 0.12,
        "min_segment_sec": 0.5,
    },
}

_spec_cache: dict[str, Any] | None = None
_spec_path_cache: Path | None = None


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def resolve_spec_path() -> Path:
    env_path = os.getenv("AFA_VEHICLE_SPEC", "").strip()
    if env_path:
        return Path(env_path)
    return DEFAULT_SPEC_PATH


def load_vehicle_spec(*, force_reload: bool = False) -> dict[str, Any]:
    global _spec_cache, _spec_path_cache
    path = resolve_spec_path()
    if (
        not force_reload
        and _spec_cache is not None
        and _spec_path_cache == path
    ):
        return _spec_cache

    spec = deepcopy(_DEFAULTS)
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                spec = _deep_merge(spec, loaded)
        except (json.JSONDecodeError, OSError):
            pass

    spec["_meta"] = {
        "path": str(path),
        "loaded": path.is_file(),
    }
    _spec_cache = spec
    _spec_path_cache = path
    return spec


def vehicle_public_spec() -> dict[str, Any]:
    spec = load_vehicle_spec()
    out = deepcopy(spec)
    out.pop("_meta", None)
    return out


def _as_float(value: Any, key: str, *, min_v: float | None = None, max_v: float | None = None) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key}는 숫자여야 합니다.") from exc
    if min_v is not None and num < min_v:
        raise ValueError(f"{key}는 {min_v} 이상이어야 합니다.")
    if max_v is not None and num > max_v:
        raise ValueError(f"{key}는 {max_v} 이하여야 합니다.")
    return num


def validate_vehicle_spec(spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise ValueError("제원 형식이 올바르지 않습니다.")

    out = vehicle_public_spec()
    out = _deep_merge(out, spec)

    out["name"] = str(out.get("name") or "A-FA").strip() or "A-FA"
    out["wheelbase_m"] = _as_float(out.get("wheelbase_m"), "wheelbase_m", min_v=0.5, max_v=10.0)
    out["track_m"] = _as_float(out.get("track_m"), "track_m", min_v=0.5, max_v=5.0)
    out["tire_radius_m"] = _as_float(out.get("tire_radius_m"), "tire_radius_m", min_v=0.1, max_v=1.0)
    out["steer_ratio"] = _as_float(out.get("steer_ratio"), "steer_ratio", min_v=1.0, max_v=40.0)
    out["mass_kg"] = _as_float(out.get("mass_kg"), "mass_kg", min_v=100.0, max_v=5000.0)

    linear = out.setdefault("linear_sensor", {})
    linear["neutral_mm"] = _as_float(linear.get("neutral_mm"), "linear neutral_mm", min_v=0.0, max_v=500.0)
    linear["mm_per_count"] = _as_float(linear.get("mm_per_count"), "mm_per_count", min_v=0.001, max_v=100.0)
    linear["visual_scale"] = _as_float(linear.get("visual_scale"), "visual_scale", min_v=0.0, max_v=1.0)

    derived = out.setdefault("derived", {})
    derived["sus_angle_gain"] = _as_float(derived.get("sus_angle_gain"), "sus_angle_gain", min_v=0.1, max_v=5.0)
    derived["yaw_from_steer"] = bool(derived.get("yaw_from_steer", True))

    vis = out.setdefault("visualization", {})
    vis["pitch_gain"] = _as_float(vis.get("pitch_gain"), "pitch_gain", min_v=0.0, max_v=2.0)
    vis["roll_gain"] = _as_float(vis.get("roll_gain"), "roll_gain", min_v=0.0, max_v=2.0)
    vis["tilt_max_deg"] = _as_float(vis.get("tilt_max_deg"), "tilt_max_deg", min_v=1.0, max_v=45.0)
    vis["sus_gain"] = _as_float(vis.get("sus_gain"), "sus_gain", min_v=0.0, max_v=3.0)
    vis["sus_tilt_max_deg"] = _as_float(vis.get("sus_tilt_max_deg"), "sus_tilt_max_deg", min_v=1.0, max_v=30.0)

    fsm = out.setdefault("fsm", {})
    for key, min_v, max_v in (
        ("stop_speed_kmh", 0.0, 30.0),
        ("brake_pressure_min", 0.0, 20.0),
        ("long_brake_g", -3.0, 0.0),
        ("corner_min_speed_kmh", 0.0, 80.0),
        ("corner_min_steer_deg", 0.0, 90.0),
        ("corner_min_lat_g", 0.0, 3.0),
        ("corner_steer_alt_deg", 0.0, 90.0),
        ("accel_throttle_min", 0.0, 100.0),
        ("accel_long_g", 0.0, 3.0),
        ("min_segment_sec", 0.1, 10.0),
    ):
        fsm[key] = _as_float(fsm.get(key), key, min_v=min_v, max_v=max_v)

    return out


def save_vehicle_spec(updates: dict[str, Any]) -> dict[str, Any]:
    validated = validate_vehicle_spec(updates)
    path = resolve_spec_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(validated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    load_vehicle_spec(force_reload=True)
    return vehicle_public_spec()
