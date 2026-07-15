from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from time import monotonic
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates

from afa_auth import install_auth
from nav_config import nav_context
from subsystem_status import (
    parse_bms_status,
    parse_inv_state,
    parse_motor_status,
    parse_vsm_state,
    public_bms_status_items,
    public_motor_status_items,
)

FORWARD_HEADER = "X-UDP-Logger-Forwarded"
LOGGER_OFFSET_URL = os.getenv(
    "LOGGER_OFFSET_URL",
    f"{os.getenv('LOGGER_INTERNAL_URL', 'http://127.0.0.1:8000').strip().rstrip('/')}/api/offset/status",
).strip()
REALTIME_PACKET_TIMES_MAXLEN = int(os.getenv("REALTIME_PACKET_TIMES_MAXLEN", os.getenv("SAMPLE_TIMES_MAXLEN", "1000")))
REALTIME_EVENTS_MAXLEN = int(os.getenv("REALTIME_EVENTS_MAXLEN", "24"))

app = FastAPI()
install_auth(app)
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

packet_times: deque[float] = deque(maxlen=max(100, REALTIME_PACKET_TIMES_MAXLEN))
recent_events: deque[dict[str, Any]] = deque(maxlen=max(8, REALTIME_EVENTS_MAXLEN))
total_packets = 0
total_samples = 0

sensor_state: dict[str, Any] = {
    "speed": 0.0,
    "accel_value": 0.0,
    "brake_value": 0.0,
    "steering_angle": 0.0,
    "steering_speed": 0.0,
    "core_temp": 0.0,
    "accel_x": 0.0,
    "accel_y": 0.0,
    "accel_z": 0.0,
    "linear_fl": 0.0,
    "linear_fr": 0.0,
    "linear_rl": 0.0,
    "linear_rr": 0.0,
    "rpm_left": 0.0,
    "rpm_right": 0.0,
    "inv_torque_feedback": 0.0,
    "inv_dc_current": 0.0,
    "bms_voltage": 0.0,
    "bms_current": 0.0,
    "bms_charge": 0.0,
    "bms_capacity": 0.0,
    "bms_ccl": 0.0,
    "bms_dcl": 0.0,
    "bms_temp_maxvalue": 0.0,
    "bms_temp_internal": 0.0,
    "bms_status": {},
    "bms_status_items": public_bms_status_items({}),
    "motor_status": {},
    "motor_status_items": public_motor_status_items(parse_motor_status(None)),
    "vsm_state": "",
    "inv_state": "",
    "device": "-",
    "updated_at": "",
}


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _nested_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _accel_dict(source: dict[str, Any]) -> dict[str, Any]:
    for key in ("accel", "accelerometer", "acceleration"):
        accel = source.get(key)
        if isinstance(accel, dict):
            return accel
    return {}


def sample_count_from_body(body: Any) -> int:
    if not isinstance(body, dict):
        return 1
    samples = body.get("samples")
    if isinstance(samples, list) and samples:
        return max(1, len(samples))
    return 1


def record_traffic(sample_count: int) -> None:
    global total_packets, total_samples
    now = monotonic()
    total_packets += 1
    total_samples += max(1, sample_count)
    for _ in range(max(1, sample_count)):
        packet_times.append(now)


def hz_last(seconds: float) -> float:
    if not packet_times:
        return 0.0
    cutoff = monotonic() - seconds
    return float(sum(1 for t in packet_times if t >= cutoff))


def _set_metric(key: str, *candidates: Any) -> bool:
    for candidate in candidates:
        value = _safe_float(candidate)
        if value is not None:
            sensor_state[key] = value
            return True
    return False


def update_sensor_state(body: Any, payload: dict[str, Any]) -> None:
    if not isinstance(body, dict):
        return

    device = str(body.get("device") or body.get("d") or payload.get("ip", "-"))
    sample: dict[str, Any] = body
    samples = body.get("samples")
    has_samples = isinstance(samples, list) and bool(samples)
    if has_samples:
        last_sample = samples[-1]
        if isinstance(last_sample, dict):
            sample = last_sample

    sample_linear = _nested_dict(sample.get("linear")) or _nested_dict(body.get("linear"))
    sample_accel = _accel_dict(sample) or _accel_dict(body)
    inverter = _nested_dict(sample.get("inverter")) or _nested_dict(body.get("inverter"))

    sensor_state["device"] = device
    sensor_state["updated_at"] = str(payload.get("time") or "")

    mapping = {
        "speed": (sample.get("speed"), body.get("speed")),
        "accel_value": (sample.get("accel_p"), sample.get("accel"), body.get("accel_p")),
        "brake_value": (sample.get("break_p"), sample.get("brake"), body.get("break_p")),
        "accel_x": (sample.get("accelX"), sample_accel.get("x"), body.get("accelX")),
        "accel_y": (sample.get("accelY"), sample_accel.get("y"), body.get("accelY")),
        "accel_z": (sample.get("accelZ"), sample_accel.get("z"), body.get("accelZ")),
        "core_temp": (sample.get("tp"), sample.get("ECU_temp"), body.get("tp")),
        "steering_angle": (sample.get("steering_angle"), body.get("steering_angle")),
        "steering_speed": (sample.get("steering_speed"), body.get("steering_speed")),
        "linear_fl": (sample.get("linear_fl"), sample_linear.get("front_left"), sample_linear.get("fl")),
        "linear_fr": (sample.get("linear_fr"), sample_linear.get("front_right"), sample_linear.get("fr")),
        "linear_rl": (sample.get("linear_rl"), sample_linear.get("rear_left"), sample_linear.get("rl")),
        "linear_rr": (sample.get("linear_rr"), sample_linear.get("rear_right"), sample_linear.get("rr")),
        "rpm_left": (sample.get("wheel_rpm"), sample.get("wheel_speed_left"), body.get("wheel_rpm")),
        "rpm_right": (sample.get("wheel_speed_right"), body.get("wheel_speed_right")),
        "inv_torque_feedback": (sample.get("INV_torque_feedback"), inverter.get("torque_feedback")),
        "inv_dc_current": (sample.get("INV_dc_currnet"), inverter.get("dc_current")),
        "bms_voltage": (sample.get("BMS_voltage"), _nested_dict(sample.get("bms")).get("voltage")),
        "bms_current": (sample.get("BMS_current"), _nested_dict(sample.get("bms")).get("current")),
    }
    for key, candidates in mapping.items():
        _set_metric(key, *candidates)

    bms = _nested_dict(sample.get("bms")) or _nested_dict(body.get("bms"))
    if bms:
        _set_metric("bms_charge", bms.get("charge"))
        _set_metric("bms_capacity", bms.get("capacity"))
        _set_metric("bms_voltage", bms.get("voltage"))
        _set_metric("bms_current", bms.get("current"))
        _set_metric("bms_ccl", bms.get("ccl"))
        _set_metric("bms_dcl", bms.get("dcl"))
        temp = _nested_dict(bms.get("temperature"))
        _set_metric("bms_temp_maxvalue", temp.get("maxvalue"), temp.get("max"))
        _set_metric("bms_temp_internal", temp.get("internal"))
        parsed_bms = parse_bms_status(bms)
        sensor_state["bms_status"] = parsed_bms
        sensor_state["bms_status_items"] = public_bms_status_items(parsed_bms)

    motor = parse_motor_status(inverter, sample)
    sensor_state["motor_status"] = motor
    sensor_state["motor_status_items"] = public_motor_status_items(motor)
    sensor_state["vsm_state"] = parse_vsm_state(inverter, sample)
    sensor_state["inv_state"] = parse_inv_state(inverter, sample)


@app.get("/")
def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="realtime.html",
        context={"request": request, **nav_context("realtime", request)},
    )


@app.post("/api/live/ingest")
async def live_ingest(request: Request) -> dict[str, Any]:
    body = await request.json()
    sample_count = sample_count_from_body(body)
    record_traffic(sample_count)

    client = request.client
    ip = client.host if client else "unknown"
    port = client.port if client else 0
    payload = {
        "time": datetime.now(timezone.utc).isoformat(),
        "ip": ip,
        "port": port,
        "message": json.dumps(body, ensure_ascii=False),
        "transport": "forward",
    }
    update_sensor_state(body, payload)
    recent_events.appendleft(
        {
            "device": sensor_state.get("device", f"{ip}:{port}"),
            "time": payload["time"],
            "transport": request.headers.get(FORWARD_HEADER, ""),
            "summary": json.dumps(body, ensure_ascii=False)[:800],
        }
    )
    return {"ok": True, "samples": sample_count}


@app.get("/api/live/snapshot")
def live_snapshot() -> dict[str, Any]:
    return {"ok": True, **sensor_state}


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    return {
        "total_packets": total_packets,
        "total_samples": total_samples,
        "hz_1s": hz_last(1.0),
        "hz_5s_avg": round(hz_last(5.0) / 5.0, 1),
        "device": sensor_state.get("device", "-"),
        "updated_at": sensor_state.get("updated_at", ""),
    }


@app.get("/api/offset/status")
def offset_status() -> dict[str, Any]:
    try:
        req = urllib.request.Request(LOGGER_OFFSET_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as exc:
        print(f"[offset] status fetch failed: {exc} url={LOGGER_OFFSET_URL}")
    from offset_shared import public_offset_status

    return {
        "ok": False,
        "message": "logger offset status unavailable",
        **public_offset_status({}),
    }
