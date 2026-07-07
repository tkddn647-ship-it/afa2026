import os
import json
import asyncio
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from nav_config import nav_context
from afa_auth import install_auth
from offset_shared import apply_offsets_to_parsed
from subsystem_status import (
    parse_bms_status,
    parse_inv_state,
    parse_motor_status,
    parse_vsm_state,
    public_bms_status_items,
    public_motor_status_items,
)

app = FastAPI()
install_auth(app)
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

sample_times: deque[float] = deque(maxlen=int(os.getenv("SAMPLE_TIMES_MAXLEN", "2000")))
recent_events: deque[dict[str, Any]] = deque(maxlen=int(os.getenv("MAX_FEED_ITEMS", "30")))
total_packets = 0
total_samples = 0
MAX_FEED_ITEMS = int(os.getenv("MAX_FEED_ITEMS", "30"))
CAMERA_INTERNAL_BASE = os.getenv("CAMERA_INTERNAL_URL", "http://127.0.0.1:8012").strip().rstrip("/")
CAMERA_PREVIEW_SOURCE = os.getenv(
    "CAMERA_PREVIEW_SOURCE",
    f"{CAMERA_INTERNAL_BASE}/api/camera/latest.jpg",
).strip()
CAMERA_STREAM_SOURCE = os.getenv(
    "CAMERA_STREAM_SOURCE",
    f"{CAMERA_INTERNAL_BASE}/api/camera/stream.mjpg",
).strip()
LOGGER_OFFSET_URL = os.getenv(
    "LOGGER_OFFSET_URL",
    f"{os.getenv('LOGGER_INTERNAL_URL', 'http://127.0.0.1:8000').strip().rstrip('/')}/api/offset/status",
).strip()
FORWARD_HEADER = "X-UDP-Logger-Forwarded"

SENSOR_UPDATE_METRIC_PATHS: dict[str, str] = {
    "car.linear.front_left": "linear_fl",
    "car.linear.front_right": "linear_fr",
    "car.linear.rear_left": "linear_rl",
    "car.linear.rear_right": "linear_rr",
    "car.accel.x": "accel_x",
    "car.accel.y": "accel_y",
    "car.accel.z": "accel_z",
}

device_offsets: dict[str, dict[str, float]] = {}
offsets_fetched_at = 0.0

sensor_state: dict[str, Any] = {
    "speed": 0,
    "accel_value": 0,
    "brake_value": 0,
    "accel_x": 0,
    "accel_y": 0,
    "accel_z": 0,
    "core_temp": 0,
    "steering_angle": 0,
    "steering_speed": 0,
    "front_tire": 0,
    "rear_tire": 0,
    "linear_fl": 0,
    "linear_fr": 0,
    "linear_rl": 0,
    "linear_rr": 0,
    "rpm_left": 0,
    "rpm_right": 0,
    "inv_temp_igt": 0,
    "inv_temp_rtd1": 0,
    "inv_temp_rtd2": 0,
    "inv_temp_gatedriver": 0,
    "inv_temp_controlboard": 0,
    "inv_temp_coolant": 0,
    "inv_temp_hotspot": 0,
    "inv_temp_motor": 0,
    "inv_motor_speed": 0,
    "inv_motor_angle": 0,
    "inv_dc_current": 0,
    "inv_a_current": 0,
    "inv_b_current": 0,
    "inv_c_current": 0,
    "inv_voltage": 0,
    "inv_voltage_output": 0,
    "inv_torque_feedback": 0,
    "inv_torque_commanded": 0,
    "inv_id_feedback": 0,
    "inv_iq_feedback": 0,
    "bms_charge": 0,
    "bms_capacity": 0,
    "bms_voltage": 0,
    "bms_current": 0,
    "bms_ccl": 0,
    "bms_dcl": 0,
    "bms_temp_maxvalue": 0,
    "bms_temp_maxid": 0,
    "bms_temp_minvalue": 0,
    "bms_temp_minid": 0,
    "bms_temp_internal": 0,
    "bms_status": {},
    "bms_status_items": public_bms_status_items({}),
    "motor_status": {},
    "motor_status_items": public_motor_status_items(parse_motor_status(None)),
    "vsm_state": "",
    "inv_state": "",
    "device": "-",
    "updated_at": "",
    "system": {
        "lv": False,
        "hv": False,
        "rtd": False,
        "err": False,
        "sd": False,
        "telemetry": False,
        "can": False,
        "imd": False,
        "bms": False,
        "bspd": False,
    },
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


def sample_count_from_body(body: Any) -> int:
    if isinstance(body, dict):
        samples = body.get("samples")
        if isinstance(samples, list):
            return max(1, len(samples))
    return 1


def summarize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    message = payload.get("message", "")
    try:
        parsed = json.loads(message)
    except Exception:
        parsed = None

    if isinstance(parsed, dict):
        device = str(parsed.get("device") or parsed.get("d") or f'{payload.get("ip", "-")}:{payload.get("port", 0)}')
        summary = json.dumps(parsed, ensure_ascii=False)
    else:
        device = f'{payload.get("ip", "-")}:{payload.get("port", 0)}'
        summary = str(message)

    return {
        "device": device,
        "time": payload.get("time", ""),
        "transport": payload.get("transport", ""),
        "summary": summary[:800],
    }


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_mcu_die_temp(*sources: Any) -> float | None:
    for source in sources:
        if not isinstance(source, dict):
            continue
        value = _safe_float(source.get("ecu_temp"))
        if value is not None:
            return value
    return None


def _nested_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _unwrap_car(source: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    car = source.get("car")
    if not isinstance(car, dict):
        return source
    merged = dict(car)
    for key, value in source.items():
        if key != "car":
            merged[key] = value
    return merged


def _accel_dict(source: dict[str, Any]) -> dict[str, Any]:
    for key in ("accel", "accelerometer", "acceleration"):
        accel = source.get(key)
        if isinstance(accel, dict):
            return accel
    return {}


def _metrics_from_sensor_updates(source: dict[str, Any]) -> dict[str, float]:
    metrics: dict[str, float] = {}
    updates = source.get("sensor_updates")
    if not isinstance(updates, list):
        return metrics
    for item in updates:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        value = _safe_float(item.get("value"))
        if not isinstance(path, str) or value is None:
            continue
        metric_key = SENSOR_UPDATE_METRIC_PATHS.get(path)
        if metric_key:
            metrics[metric_key] = value
    return metrics


def _set_sensor_metric(key: str, *candidates: Any) -> bool:
    for candidate in candidates:
        value = _safe_float(candidate)
        if value is not None:
            sensor_state[key] = value
            return True
    return False


def _update_inverter_fields(sample: dict[str, Any], body: dict[str, Any]) -> None:
    inverter = _nested_dict(sample.get("inverter")) or _nested_dict(body.get("inverter"))
    if inverter:
        temperature = _nested_dict(inverter.get("temperature"))
        if temperature:
            igbt = _nested_dict(temperature.get("igbt"))
            if igbt:
                igbt_max = _nested_dict(igbt.get("max"))
                if igbt_max:
                    _set_sensor_metric("inv_temp_igt", igbt_max.get("temperature"))
            rtd = _nested_dict(temperature.get("rtd"))
            if rtd:
                _set_sensor_metric("inv_temp_rtd1", rtd.get("rtd1"))
                _set_sensor_metric("inv_temp_rtd2", rtd.get("rtd2"))
            _set_sensor_metric(
                "inv_temp_gatedriver",
                temperature.get("gatedriver"),
            )
            _set_sensor_metric(
                "inv_temp_controlboard",
                temperature.get("controlboard"),
            )
            _set_sensor_metric("inv_temp_coolant", temperature.get("coolant"))
            _set_sensor_metric("inv_temp_hotspot", temperature.get("hotspot"))
            _set_sensor_metric("inv_temp_motor", temperature.get("motor"))

        motor = _nested_dict(inverter.get("motor"))
        if motor:
            _set_sensor_metric("inv_motor_speed", motor.get("speed"))
            _set_sensor_metric("inv_motor_angle", motor.get("angle"))

        current = _nested_dict(inverter.get("current"))
        if current:
            _set_sensor_metric("inv_dc_current", current.get("dc_bus"))
            _set_sensor_metric("inv_a_current", current.get("A"))
            _set_sensor_metric("inv_b_current", current.get("B"))
            _set_sensor_metric("inv_c_current", current.get("C"))

        voltage = _nested_dict(inverter.get("voltage"))
        if voltage:
            _set_sensor_metric("inv_voltage", voltage.get("dc_bus"))
            _set_sensor_metric("inv_voltage_output", voltage.get("output"))

        torque = _nested_dict(inverter.get("torque"))
        if torque:
            _set_sensor_metric("inv_torque_feedback", torque.get("feedback"))
            _set_sensor_metric("inv_torque_commanded", torque.get("commanded"))

        feedback = _nested_dict(inverter.get("feedback"))
        if feedback:
            _set_sensor_metric("inv_id_feedback", feedback.get("id"))
            _set_sensor_metric("inv_iq_feedback", feedback.get("iq"))

    flat_aliases: dict[str, tuple[str, ...]] = {
        "inv_temp_igt": ("INV_TEMP_IGT", "inv_temp_igt"),
        "inv_temp_rtd1": ("INV_TEMP_RTD1", "inv_temp_rtd1"),
        "inv_temp_rtd2": ("INV_TEMP_RTD2", "inv_temp_rtd2"),
        "inv_temp_gatedriver": ("INV_TEMP_gatedriver", "inv_temp_gatedriver"),
        "inv_temp_controlboard": ("INV_TEMP_controlboard", "inv_temp_controlboard"),
        "inv_temp_coolant": ("INV_TEMP_coolant", "inv_temp_coolant"),
        "inv_temp_hotspot": ("INV_TEMP_hotspot", "inv_temp_hotspot"),
        "inv_temp_motor": ("INV_TEMP_motor", "inv_temp_motor"),
        "inv_motor_speed": ("INV_moter_speed", "INV_motor_speed", "inv_motor_speed", "inv_moter_speed"),
        "inv_motor_angle": ("INV_moter_angle", "INV_motor_angle", "inv_motor_angle", "inv_moter_angle"),
        "inv_dc_current": ("INV_dc_currnet", "INV_dc_current", "inv_dc_current", "inv_dc_currnet"),
        "inv_a_current": ("INV_A_currnet", "INV_A_current", "inv_a_current", "inv_a_currnet"),
        "inv_b_current": ("INV_B_currnet", "INV_B_current", "inv_b_current", "inv_b_currnet"),
        "inv_c_current": ("INV_C_currnet", "INV_C_current", "inv_c_current", "inv_c_currnet"),
        "inv_voltage": ("INV_voltage", "inv_voltage"),
        "inv_voltage_output": ("INV_voltage_output", "inv_voltage_output"),
        "inv_torque_feedback": ("INV_torque_feedback", "inv_torque_feedback"),
        "inv_torque_commanded": ("INV_torque_commanded", "inv_torque_commanded"),
        "inv_id_feedback": ("INV_id_feedback", "inv_id_feedback"),
        "inv_iq_feedback": ("INV_iq_feedback", "inv_iq_feedback"),
    }
    for key, names in flat_aliases.items():
        candidates: list[Any] = []
        for name in names:
            candidates.append(sample.get(name))
            candidates.append(body.get(name))
        _set_sensor_metric(key, *candidates)

    sensor_state["motor_status"] = parse_motor_status(inverter, sample)
    sensor_state["motor_status_items"] = public_motor_status_items(sensor_state["motor_status"])
    sensor_state["vsm_state"] = parse_vsm_state(inverter, sample)
    sensor_state["inv_state"] = parse_inv_state(inverter, sample)


def _update_bms_fields(sample: dict[str, Any], body: dict[str, Any]) -> None:
    bms = _nested_dict(sample.get("bms")) or _nested_dict(body.get("bms"))
    if bms:
        _set_sensor_metric("bms_charge", bms.get("charge"))
        _set_sensor_metric("bms_capacity", bms.get("capacity"))
        _set_sensor_metric("bms_voltage", bms.get("voltage"))
        _set_sensor_metric("bms_current", bms.get("current"))
        _set_sensor_metric("bms_ccl", bms.get("ccl"))
        _set_sensor_metric("bms_dcl", bms.get("dcl"))
        temperature = _nested_dict(bms.get("temperature"))
        if temperature:
            temp_max = _nested_dict(temperature.get("max"))
            if temp_max:
                _set_sensor_metric("bms_temp_maxvalue", temp_max.get("value"))
                _set_sensor_metric("bms_temp_maxid", temp_max.get("id"))
            temp_min = _nested_dict(temperature.get("min"))
            if temp_min:
                _set_sensor_metric("bms_temp_minvalue", temp_min.get("value"))
                _set_sensor_metric("bms_temp_minid", temp_min.get("id"))
            _set_sensor_metric("bms_temp_internal", temperature.get("internal"))
        sensor_state["bms_status"] = parse_bms_status(bms)
        sensor_state["bms_status_items"] = public_bms_status_items(sensor_state["bms_status"])

    flat_aliases: dict[str, tuple[str, ...]] = {
        "bms_charge": ("BMS_charge", "bms_charge"),
        "bms_capacity": ("BMS_capacity", "bms_capacity"),
        "bms_voltage": ("BMS_voltage", "bms_voltage"),
        "bms_current": ("BMS_current", "bms_current"),
        "bms_ccl": ("BMS_ccl", "bms_ccl"),
        "bms_dcl": ("BMS_dcl", "bms_dcl"),
        "bms_temp_maxvalue": ("BMS_TEMP_maxvalue", "bms_temp_maxvalue"),
        "bms_temp_maxid": ("BMS_TEMP_maxid", "bms_temp_maxid"),
        "bms_temp_minvalue": ("BMS_TEMP_minvalue", "bms_temp_minvalue"),
        "bms_temp_minid": ("BMS_TEMP_minid", "bms_temp_minid"),
        "bms_temp_internal": ("BMS_TEMP_internal", "bms_temp_internal"),
    }
    for key, names in flat_aliases.items():
        candidates: list[Any] = []
        for name in names:
            candidates.append(sample.get(name))
            candidates.append(body.get(name))
        _set_sensor_metric(key, *candidates)


def refresh_device_offsets(force: bool = False) -> None:
    global device_offsets, offsets_fetched_at
    now = monotonic()
    if not force and now - offsets_fetched_at < 0.5:
        return
    request = urllib.request.Request(
        LOGGER_OFFSET_URL,
        headers={"Accept": "application/json", "Cache-Control": "no-cache"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=1.5) as response:
            data = json.loads(response.read().decode("utf-8"))
        if isinstance(data, dict):
            offsets = data.get("offsets")
            if isinstance(offsets, dict):
                device_offsets = offsets
        offsets_fetched_at = now
    except Exception as exc:
        print(f"[offset] refresh failed: {exc} url={LOGGER_OFFSET_URL}")


def _device_key_from_body(body: dict[str, Any], ip: str, port: int) -> str:
    return str(body.get("device") or body.get("d") or f"{ip}:{port}")


def _offsets_for_device(device_key: str) -> dict[str, float]:
    offsets = device_offsets.get(device_key)
    if isinstance(offsets, dict) and offsets:
        return offsets
    if ":" in device_key:
        ip = device_key.split(":", 1)[0]
        for key, value in device_offsets.items():
            if key.startswith(f"{ip}:") and isinstance(value, dict) and value:
                return value
    return {}


def apply_live_offsets(body: Any, ip: str, port: int) -> Any:
    if not isinstance(body, dict):
        return body
    refresh_device_offsets()
    offsets = _offsets_for_device(_device_key_from_body(body, ip, port))
    if offsets:
        apply_offsets_to_parsed(body, offsets)
    return body


def update_sensor_state(body: Any, payload: dict[str, Any]) -> None:
    if not isinstance(body, dict):
        return

    body = _unwrap_car(body)
    device = str(body.get("device") or body.get("d") or payload.get("ip", "-"))
    sample = body
    samples = body.get("samples")
    has_samples = isinstance(samples, list) and bool(samples)
    if has_samples:
        last_sample = samples[-1]
        if isinstance(last_sample, dict):
            sample = _unwrap_car(last_sample)

    sample_linear = _nested_dict(sample.get("linear"))
    if not sample_linear:
        sample_linear = _nested_dict(body.get("linear"))

    sample_accel = _accel_dict(sample) or _accel_dict(body)
    sample_steering = _nested_dict(sample.get("steering")) or _nested_dict(body.get("steering"))
    update_metrics = _metrics_from_sensor_updates(sample)
    if not update_metrics:
        update_metrics = _metrics_from_sensor_updates(body)
    mcu_die_temp = _extract_mcu_die_temp(sample, body)

    sensor_state["device"] = device
    sensor_state["updated_at"] = payload["time"]

    mapping = {
        "speed": (sample.get("speed"), body.get("speed")),
        "accel_value": (
            sample.get("accel_p"),
            _safe_float(sample.get("accel")),
            body.get("accel_p"),
            _safe_float(body.get("accel")),
        ),
        "brake_value": (
            sample.get("break_p"),
            sample.get("brake"),
            body.get("break_p"),
            _safe_float(body.get("brake")),
        ),
        "accel_x": (
            sample.get("accelX"),
            sample_accel.get("x"),
            sample_accel.get("ax"),
            body.get("accelX"),
            update_metrics.get("accel_x"),
        ),
        "accel_y": (
            sample.get("accelY"),
            sample_accel.get("y"),
            sample_accel.get("ay"),
            body.get("accelY"),
            update_metrics.get("accel_y"),
        ),
        "accel_z": (
            sample.get("accelZ"),
            sample_accel.get("z"),
            sample_accel.get("az"),
            body.get("accelZ"),
            update_metrics.get("accel_z"),
        ),
        "core_temp": (mcu_die_temp,),
        "steering_angle": (sample.get("steering_angle"), sample_steering.get("angle"), sample.get("angle"), body.get("steering_angle")),
        "steering_speed": (sample.get("steering_speed"), sample_steering.get("speed"), body.get("steering_speed")),
        "front_tire": (sample.get("front_Tire"), sample.get("front_tire"), body.get("front_Tire")),
        "rear_tire": (sample.get("rear_Tire"), sample.get("rear_tire"), body.get("rear_Tire")),
        "linear_fl": (
            sample.get("linear_fl"),
            sample_linear.get("front_left"),
            sample_linear.get("fl"),
            update_metrics.get("linear_fl"),
        ),
        "linear_fr": (
            sample.get("linear_fr"),
            sample_linear.get("front_right"),
            sample_linear.get("fr"),
            update_metrics.get("linear_fr"),
        ),
        "linear_rl": (
            sample.get("linear_rl"),
            sample_linear.get("rear_left"),
            sample_linear.get("rl"),
            update_metrics.get("linear_rl"),
        ),
        "linear_rr": (
            sample.get("linear_rr"),
            sample_linear.get("rear_right"),
            sample_linear.get("rr"),
            update_metrics.get("linear_rr"),
        ),
        "rpm_left": (
            sample.get("wheel_speed_left"),
            body.get("wheel_speed_left"),
            sample.get("wheel_rpm"),
            body.get("wheel_rpm"),
        ),
        "rpm_right": (sample.get("wheel_speed_right"), body.get("wheel_speed_right")),
    }

    metric_keys = set(mapping)
    updated_keys: set[str] = set()
    for key, candidates in mapping.items():
        for candidate in candidates:
            value = _safe_float(candidate)
            if value is not None:
                sensor_state[key] = value
                updated_keys.add(key)
                break

    if has_samples:
        for key in metric_keys - updated_keys:
            if key == "core_temp":
                continue
            sensor_state[key] = 0

    _update_inverter_fields(sample, body)
    _update_bms_fields(sample, body)

    system = sample.get("system") if isinstance(sample.get("system"), dict) else body.get("system")
    if isinstance(system, dict):
        current = dict(sensor_state.get("system") or {})
        for flag in ("lv", "hv", "rtd", "err", "sd", "telemetry", "can", "imd", "bms", "bspd"):
            if flag in system:
                current[flag] = bool(system.get(flag))
        sensor_state["system"] = current
    else:
        live = True
        sensor_state["system"] = {
            "lv": live,
            "hv": live,
            "rtd": live,
            "err": False,
            "sd": live,
            "telemetry": live,
            "can": live,
            "imd": live,
            "bms": live,
            "bspd": live,
        }


@app.get("/")
def home(request: Request):
    template_path = BASE_DIR / "templates" / "realtime.html"
    if template_path.exists():
        return templates.TemplateResponse(
            request=request,
            name="realtime.html",
            context={"request": request, **nav_context("realtime", request)},
        )

    return HTMLResponse(
        """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sensor Logger</title>
  <style>
    body { margin: 0; font-family: Arial, sans-serif; }
    main { padding: 16px; }
  </style>
</head>
<body>
  <main>
    <h1>Sensor Logger</h1>
    <pre id="snapshot">loading...</pre>
  </main>
  <script>
    async function tick() {
      const data = await fetch('/api/live/snapshot', { cache: 'no-store' }).then(r => r.json());
      document.getElementById('snapshot').textContent = JSON.stringify(data, null, 2);
      setTimeout(tick, 200);
    }
    tick();
  </script>
</body>
</html>
        """.strip()
    )


@app.on_event("startup")
async def realtime_startup_event() -> None:
    async def offset_refresh_loop() -> None:
        while True:
            await asyncio.to_thread(refresh_device_offsets, True)
            await asyncio.sleep(1.0)

    asyncio.create_task(offset_refresh_loop())


@app.post("/api/live/ingest")
async def live_ingest(request: Request) -> dict[str, Any]:
    body = await request.json()
    sample_count = sample_count_from_body(body)

    client = request.client
    ip = client.host if client else "unknown"
    port = client.port if client else 0
  # 로깅 서버가 이미 오프셋을 적용한 패킷은 다시 빼지 않는다.
    if request.headers.get(FORWARD_HEADER) != "1":
        body = apply_live_offsets(body, ip, port)
    record_traffic(sample_count)
    payload = {
        "time": datetime.now(timezone.utc).isoformat(),
        "ip": ip,
        "port": port,
        "message": json.dumps(body, ensure_ascii=False),
        "transport": "forward",
    }
    update_sensor_state(body, payload)
    if isinstance(body, dict):
        device = str(body.get("device") or body.get("d") or f"{ip}:{port}")
        summary = json.dumps(body, ensure_ascii=False)[:800]
    else:
        device = f"{ip}:{port}"
        summary = str(body)[:800]
    recent_events.appendleft(
        {
            "device": device,
            "time": payload["time"],
            "transport": "forward",
            "summary": summary,
        }
    )
    return {"ok": True, "samples": sample_count}


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    return {
        "ok": True,
        "total_packets": total_packets,
        "total_samples": total_samples,
        "hz_1s": hz_last(1.0),
        "hz_5s_avg": round(hz_last(5.0) / 5.0, 1),
    }


@app.get("/api/live/recent")
def live_recent() -> dict[str, Any]:
    return {
        "ok": True,
        "items": list(recent_events),
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


@app.get("/api/live/snapshot")
def live_snapshot() -> dict[str, Any]:
    return {
        "ok": True,
        **sensor_state,
    }


@app.get("/api/camera/preview.jpg")
def camera_preview() -> Response:
    source_url = f"{CAMERA_PREVIEW_SOURCE}?_={int(monotonic() * 1000)}"
    request = urllib.request.Request(
        source_url,
        headers={"Cache-Control": "no-cache"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=2.5) as upstream:
            body = upstream.read()
            if not body:
                return Response(status_code=204)
            media_type = upstream.headers.get("Content-Type", "image/jpeg")
            headers = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
            for header_name in ("X-Camera-Seq", "X-Camera-Updated-At", "X-Camera-Source"):
                header_value = upstream.headers.get(header_name)
                if header_value:
                    headers[header_name] = header_value
            return Response(content=body, media_type=media_type, headers=headers)
    except urllib.error.HTTPError as exc:
        if exc.code == 204:
            return Response(status_code=204)
        return Response(status_code=502)
    except Exception:
        return Response(status_code=502)


def _open_camera_upstream(url: str, *, timeout: float = 60.0) -> tuple[Any, str]:
    upstream = urllib.request.urlopen(
        urllib.request.Request(url, headers={"Cache-Control": "no-cache"}),
        timeout=timeout,
    )
    media_type = upstream.headers.get("Content-Type", "")
    return upstream, media_type


@app.get("/api/camera/stream.mjpg")
async def camera_stream_proxy() -> StreamingResponse:
    async def generate():
        while True:
            upstream = None
            try:
                upstream, _ = await asyncio.to_thread(_open_camera_upstream, CAMERA_STREAM_SOURCE)
                while True:
                    chunk = await asyncio.to_thread(upstream.read, 65536)
                    if not chunk:
                        break
                    yield chunk
            except Exception:
                await asyncio.sleep(0.3)
            finally:
                if upstream is not None:
                    upstream.close()

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


if __name__ == "__main__":
    try:
        import uvicorn
    except ModuleNotFoundError:
        print("[error] uvicorn is not installed.")
        print("[hint] Install dependencies first: pip install fastapi uvicorn jinja2")
        raise SystemExit(1)

    port = int(os.getenv("PORT", "8011"))
    uvicorn.run("realtime_server:app", host="0.0.0.0", port=port, reload=False)
