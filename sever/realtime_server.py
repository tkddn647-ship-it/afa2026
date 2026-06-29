import json
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

app = FastAPI()
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

sample_times: deque[float] = deque(maxlen=20000)
recent_events: deque[dict[str, Any]] = deque(maxlen=100)
total_packets = 0
total_samples = 0
MAX_FEED_ITEMS = int(os.getenv("MAX_FEED_ITEMS", "30"))

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
    cutoff = now - 5.0
    while sample_times and sample_times[0] < cutoff:
        sample_times.popleft()


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


def _accel_dict(source: dict[str, Any]) -> dict[str, Any]:
    for key in ("accel", "accelerometer", "acceleration"):
        accel = source.get(key)
        if isinstance(accel, dict):
            return accel
    return {}


def update_sensor_state(body: Any, payload: dict[str, Any]) -> None:
    if not isinstance(body, dict):
        return

    device = str(body.get("device") or body.get("d") or payload.get("ip", "-"))
    sample = body
    samples = body.get("samples")
    has_samples = isinstance(samples, list) and bool(samples)
    if has_samples:
        last_sample = samples[-1]
        if isinstance(last_sample, dict):
            sample = last_sample

    sample_linear = _nested_dict(sample.get("linear"))
    if not sample_linear:
        sample_linear = _nested_dict(body.get("linear"))

    sample_accel = _accel_dict(sample) or _accel_dict(body)
    sample_steering = _nested_dict(sample.get("steering")) or _nested_dict(body.get("steering"))
    sample_wheel = _nested_dict(sample.get("wheel")) or _nested_dict(body.get("wheel"))
    mcu_die_temp = _extract_mcu_die_temp(sample, body)

    sensor_state["device"] = device
    sensor_state["updated_at"] = payload["time"]

    mapping = {
        "speed": (sample.get("speed"), body.get("speed")),
        "accel_value": (sample.get("accel_p"), sample.get("accel"), body.get("accel_p")),
        "brake_value": (sample.get("break_p"), sample.get("brake"), body.get("break_p")),
        "accel_x": (
            sample.get("accelX"),
            sample_accel.get("x"),
            sample_accel.get("ax"),
            body.get("accelX"),
        ),
        "accel_y": (
            sample.get("accelY"),
            sample_accel.get("y"),
            sample_accel.get("ay"),
            body.get("accelY"),
        ),
        "accel_z": (
            sample.get("accelZ"),
            sample_accel.get("z"),
            sample_accel.get("az"),
            body.get("accelZ"),
        ),
        "core_temp": (mcu_die_temp,),
        "steering_angle": (sample.get("steering_angle"), sample_steering.get("angle"), sample.get("angle"), body.get("steering_angle")),
        "steering_speed": (sample.get("steering_speed"), sample_steering.get("speed"), body.get("steering_speed")),
        "front_tire": (sample.get("front_Tire"), sample.get("front_tire"), body.get("front_Tire")),
        "rear_tire": (sample.get("rear_Tire"), sample.get("rear_tire"), body.get("rear_Tire")),
        "linear_fl": (sample.get("linear_fl"), sample_linear.get("front_left"), sample_linear.get("fl")),
        "linear_fr": (sample.get("linear_fr"), sample_linear.get("front_right"), sample_linear.get("fr")),
        "linear_rl": (sample.get("linear_rl"), sample_linear.get("rear_left"), sample_linear.get("rl")),
        "linear_rr": (sample.get("linear_rr"), sample_linear.get("rear_right"), sample_linear.get("rr")),
        "rpm_left": (
            sample.get("wheel_rpm_left"),
            sample_wheel.get("rpm_left"),
            sample.get("wheel_speed_left"),
            body.get("wheel_rpm_left"),
            body.get("wheel_speed_left"),
            sample.get("wheel_rpm"),
            body.get("wheel_rpm"),
        ),
        "rpm_right": (
            sample.get("wheel_rpm_right"),
            sample_wheel.get("rpm_right"),
            sample.get("wheel_speed_right"),
            body.get("wheel_rpm_right"),
            body.get("wheel_speed_right"),
        ),
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
            context={"request": request},
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
    recent_events.appendleft(summarize_payload(payload))
    while len(recent_events) > MAX_FEED_ITEMS:
        recent_events.pop()
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


@app.get("/api/live/snapshot")
def live_snapshot() -> dict[str, Any]:
    return {
        "ok": True,
        **sensor_state,
    }


if __name__ == "__main__":
    try:
        import uvicorn
    except ModuleNotFoundError:
        print("[error] uvicorn is not installed.")
        print("[hint] Install dependencies first: pip install fastapi uvicorn jinja2")
        raise SystemExit(1)

    port = int(os.getenv("PORT", "8011"))
    uvicorn.run("realtime_server:app", host="0.0.0.0", port=port, reload=False)
