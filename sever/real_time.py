import json
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

app = FastAPI()
BASE_DIR = Path(__file__).resolve().parent

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
        return float(value)
    except (TypeError, ValueError):
        return None


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


def update_sensor_state(body: Any, payload: dict[str, Any]) -> None:
    if not isinstance(body, dict):
        return

    device = str(body.get("device") or body.get("d") or payload.get("ip", "-"))
    sample = body
    samples = body.get("samples")
    if isinstance(samples, list) and samples:
        last_sample = samples[-1]
        if isinstance(last_sample, dict):
            sample = last_sample

    linear = body.get("linear") if isinstance(body.get("linear"), dict) else {}
    mcu_die_temp = _extract_mcu_die_temp(sample, body)
    sensor_state["device"] = device
    sensor_state["updated_at"] = payload["time"]

    mapping = {
        "speed": (sample.get("speed"), body.get("speed")),
        "accel_value": (sample.get("accel_p"), sample.get("accel"), body.get("accel_p")),
        "brake_value": (sample.get("break_p"), sample.get("brake"), body.get("break_p")),
        "accel_x": (sample.get("accelX"), body.get("accelX"), body.get("accel", {}).get("x") if isinstance(body.get("accel"), dict) else None),
        "accel_y": (sample.get("accelY"), body.get("accelY"), body.get("accel", {}).get("y") if isinstance(body.get("accel"), dict) else None),
        "accel_z": (sample.get("accelZ"), body.get("accelZ"), body.get("accel", {}).get("z") if isinstance(body.get("accel"), dict) else None),
        "core_temp": (mcu_die_temp,),
        "steering_angle": (sample.get("steering_angle"), sample.get("angle"), body.get("steering_angle")),
        "steering_speed": (sample.get("steering_speed"), sample.get("speed"), body.get("steering_speed")),
        "front_tire": (sample.get("front_Tire"), sample.get("front_tire"), body.get("front_Tire")),
        "rear_tire": (sample.get("rear_Tire"), sample.get("rear_tire"), body.get("rear_Tire")),
        "linear_fl": (sample.get("linear_fl"), linear.get("front_left"), linear.get("fl")),
        "linear_fr": (sample.get("linear_fr"), linear.get("front_right"), linear.get("fr")),
        "linear_rl": (sample.get("linear_rl"), linear.get("rear_left"), linear.get("rl")),
        "linear_rr": (sample.get("linear_rr"), linear.get("rear_right"), linear.get("rr")),
        "rpm_left": (
            sample.get("wheel_speed_left"),
            body.get("wheel_speed_left"),
            sample.get("wheel_rpm"),
            body.get("wheel_rpm"),
        ),
        "rpm_right": (sample.get("wheel_speed_right"), body.get("wheel_speed_right")),
    }

    for key, candidates in mapping.items():
        for candidate in candidates:
            value = _safe_float(candidate)
            if value is not None:
                sensor_state[key] = value
                break


@app.get("/")
def home(request: Request):
    return HTMLResponse(
        """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sensor Logger</title>
  <style>
    :root { color-scheme: dark; --bg:#070b12; --panel:#101827; --line:#22324a; --text:#f6f8fc; --muted:#8fb2df; }
    * { box-sizing: border-box; }
    body { margin: 0; background: radial-gradient(circle at top left, #10213a 0, var(--bg) 42%); color: var(--text); font-family: Arial, sans-serif; }
    .page { width: min(1320px, 100%); margin: 0 auto; padding: 28px; }
    h1 { margin: 0 0 8px; font-size: 42px; line-height: 1; }
    .sub { color: var(--muted); margin-bottom: 22px; }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
    .main { display: grid; grid-template-columns: 1.2fr 1fr; gap: 16px; margin-top: 16px; }
    .card { background: linear-gradient(180deg, #101827, #0b111c); border: 1px solid var(--line); border-radius: 8px; padding: 16px; min-width: 0; }
    .label { color: var(--muted); font-size: 12px; margin-bottom: 12px; }
    .value { font-size: 30px; font-weight: 800; line-height: 1; }
    .unit { color: var(--muted); font-size: 14px; margin-left: 4px; }
    .speed-card { min-height: 300px; display: grid; place-items: center; text-align: center; }
    .speed { font-size: 92px; font-weight: 900; line-height: 1; }
    .rows { display: grid; gap: 10px; }
    .row { display: flex; justify-content: space-between; align-items: center; gap: 16px; padding: 13px 0; border-bottom: 1px solid rgba(143,178,223,.16); }
    .row:last-child { border-bottom: 0; }
    .row strong { font-size: 22px; }
    .linear { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .tile { padding: 14px; border-radius: 8px; background: #090f18; border: 1px solid #1a2638; }
    .recent { max-height: 260px; overflow: auto; color: #c8d8ec; font-size: 13px; line-height: 1.45; }
    .event { padding: 10px 0; border-bottom: 1px solid rgba(143,178,223,.14); }
    @media (max-width: 960px) { .page { padding: 18px; } .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .main { grid-template-columns: 1fr; } h1 { font-size: 34px; } }
  </style>
</head>
<body>
  <div class="page">
    <h1>실시간 모니터링</h1>
    <div class="sub">센서 데이터 전용 대시보드입니다. 카메라는 8012 카메라 서버에서 별도로 확인합니다.</div>

    <section class="grid">
      <div class="card"><div class="label">상태</div><div class="value" id="status">polling</div></div>
      <div class="card"><div class="label">1초 수신량</div><div class="value"><span id="hz1">0</span><span class="unit">Hz</span></div></div>
      <div class="card"><div class="label">5초 평균</div><div class="value"><span id="hz5">0.0</span><span class="unit">Hz</span></div></div>
      <div class="card"><div class="label">최근 디바이스</div><div class="value" id="device">-</div></div>
    </section>

    <section class="main">
      <div class="card speed-card">
        <div><div class="label">차속</div><div class="speed"><span id="speed">0</span></div><div class="unit">km/h</div></div>
      </div>
      <div class="card rows">
        <div class="row"><span>엑셀</span><strong><span id="accelValue">0</span></strong></div>
        <div class="row"><span>브레이크</span><strong><span id="brakeValue">0</span></strong></div>
        <div class="row"><span>스티어링 각도</span><strong><span id="steeringAngle">0</span>°</strong></div>
        <div class="row"><span>스티어링 속도</span><strong><span id="steeringSpeed">0</span>°/s</strong></div>
        <div class="row"><span>MCU 칩 내부 온도</span><strong><span id="coreTemp">0</span> °C</strong></div>
      </div>
    </section>

    <section class="main">
      <div class="card">
        <div class="label">linear / wheel</div>
        <div class="linear">
          <div class="tile"><div class="label">FL</div><div class="value"><span id="linearFl">0</span><span class="unit">mm</span></div><div class="unit">L <span id="rpmLeftA">0</span> rpm</div></div>
          <div class="tile"><div class="label">FR</div><div class="value"><span id="linearFr">0</span><span class="unit">mm</span></div><div class="unit">R <span id="rpmRightA">0</span> rpm</div></div>
          <div class="tile"><div class="label">RL</div><div class="value"><span id="linearRl">0</span><span class="unit">mm</span></div><div class="unit">L <span id="rpmLeftB">0</span> rpm</div></div>
          <div class="tile"><div class="label">RR</div><div class="value"><span id="linearRr">0</span><span class="unit">mm</span></div><div class="unit">R <span id="rpmRightB">0</span> rpm</div></div>
        </div>
      </div>
      <div class="card"><div class="label">최근 수신</div><div class="recent" id="recent">waiting...</div></div>
    </section>
  </div>
  <script>
    const text = (id, value, digits = 0) => {
      const node = document.getElementById(id);
      if (!node) return;
      const number = Number(value || 0);
      node.textContent = Number.isFinite(number) ? number.toFixed(digits) : String(value || 0);
    };
    async function tick() {
      try {
        const [snapshot, stats, recent] = await Promise.all([
          fetch('/api/live/snapshot', { cache: 'no-store' }).then(r => r.json()),
          fetch('/api/stats', { cache: 'no-store' }).then(r => r.json()),
          fetch('/api/live/recent', { cache: 'no-store' }).then(r => r.json())
        ]);
        document.getElementById('status').textContent = stats.hz_1s > 0 ? 'online' : 'polling';
        document.getElementById('device').textContent = snapshot.device || '-';
        text('hz1', stats.hz_1s, 0);
        text('hz5', stats.hz_5s_avg, 1);
        text('speed', snapshot.speed, 0);
        text('accelValue', snapshot.accel_value, 0);
        text('brakeValue', snapshot.brake_value, 0);
        text('steeringAngle', snapshot.steering_angle, 0);
        text('steeringSpeed', snapshot.steering_speed, 0);
        text('coreTemp', snapshot.core_temp, 1);
        text('linearFl', snapshot.linear_fl, 0);
        text('linearFr', snapshot.linear_fr, 0);
        text('linearRl', snapshot.linear_rl, 0);
        text('linearRr', snapshot.linear_rr, 0);
        text('rpmLeftA', snapshot.rpm_left, 0);
        text('rpmLeftB', snapshot.rpm_left, 0);
        text('rpmRightA', snapshot.rpm_right, 0);
        text('rpmRightB', snapshot.rpm_right, 0);
        const events = recent.items || [];
        document.getElementById('recent').innerHTML = events.length
          ? events.slice(0, 8).map(item => `<div class="event"><strong>${item.device || '-'}</strong><br>${item.summary || ''}</div>`).join('')
          : 'waiting...';
      } catch (err) {
        document.getElementById('status').textContent = 'error';
      } finally {
        setTimeout(tick, 200);
      }
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
    uvicorn.run("real_time:app", host="0.0.0.0", port=port, reload=False)
