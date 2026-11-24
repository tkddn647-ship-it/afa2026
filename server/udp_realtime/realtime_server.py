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
from vehicle_speed import motor_rpm_to_speed_kmh

FORWARD_HEADER = "X-UDP-Logger-Forwarded"
LOGGER_OFFSET_URL = os.getenv(
    "LOGGER_OFFSET_URL",
    f"{os.getenv('LOGGER_INTERNAL_URL', 'http://127.0.0.1:8000').strip().rstrip('/')}/api/offset/status",
).strip()
REALTIME_PACKET_TIMES_MAXLEN = int(os.getenv("REALTIME_PACKET_TIMES_MAXLEN", os.getenv("SAMPLE_TIMES_MAXLEN", "2500")))
REALTIME_EVENTS_MAXLEN = int(os.getenv("REALTIME_EVENTS_MAXLEN", "24"))

app = FastAPI()
install_auth(app)
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

packet_times: deque[float] = deque(maxlen=max(100, REALTIME_PACKET_TIMES_MAXLEN))
recent_events: deque[dict[str, Any]] = deque(maxlen=max(8, REALTIME_EVENTS_MAXLEN))
total_packets = 0
total_samples = 0
last_traffic_mono: float = 0.0
last_stm_ms_seen: float | None = None
last_stm_advance_mono: float = 0.0
last_ui_publish_mono: float = 0.0
LIVE_STALE_SEC = float(os.getenv("LIVE_STALE_SEC", "1.5"))
# offline로 잠깐 끊겨도 직전 값을 유지. 이 시간 지나야 화면 값을 0으로 비움.
LIVE_CLEAR_SEC = float(os.getenv("LIVE_CLEAR_SEC", "5.0"))
# 화면/차트용 publish 상한 (수신은 더 빨라도 최신값만 5Hz로 반영)
REALTIME_UI_HZ = max(1.0, float(os.getenv("REALTIME_UI_HZ", "5")))
UI_PUBLISH_INTERVAL_SEC = 1.0 / REALTIME_UI_HZ

sensor_state: dict[str, Any] = {
    "stm_ms": 0.0,
    "FR": 0.0,
    "FL": 0.0,
    "RR": 0.0,
    "RL": 0.0,
    "x_g": 0.0,
    "y_g": 0.0,
    "z_g": 0.0,
    "ecu_temp": 0.0,
    "steering_angle": 0.0,
    "steering_speed": 0.0,
    "inv_temp_igt": 0.0,
    "inv_temp_motor": 0.0,
    "inv_motor_speed": 0.0,
    "vehicle_speed_kmh": 0.0,
    "inv_dc_current": 0.0,
    "inv_voltage": 0.0,
    "INV_shudder_torque": 0.0,
    "INV_torque_feedback": 0.0,
    "INV_torque_commanded": 0.0,
    "INV_id_feedback": 0.0,
    "INV_iq_feedback": 0.0,
    "INV_id_command": 0.0,
    "INV_iq_command": 0.0,
    "motor_precharge": False,
    "motor_main_contactor": False,
    "motor_inverter_mode": 0,  # 0=Torque, 1=Speed
    "motor_inverter_mode_label": "Torque",
    "can1": 0,
    "can2": 0,
    "bms_charge": 0.0,
    "bms_voltage": 0.0,
    "bms_current": 0.0,
    "bms_ccl": 0.0,
    "bms_dcl": 0.0,
    "bms_temp_maxvalue": 0.0,
    "bms_capacity": 0.0,
    "bms_status": {},
    "bms_status_items": public_bms_status_items({}),
    "motor_status": {},
    "motor_status_items": public_motor_status_items(parse_motor_status(None)),
    "vsm_state": "",
    "inv_state": "",
    # 하위 호환 별칭 (구 UI / 카메라 HUD)
    "accel_x": 0.0,
    "accel_y": 0.0,
    "accel_z": 0.0,
    "core_temp": 0.0,
    "linear_fl": 0.0,
    "linear_fr": 0.0,
    "linear_rl": 0.0,
    "linear_rr": 0.0,
    "motor_rpm": 0.0,
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


def _pick(sample: dict[str, Any], body: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in sample and sample.get(key) is not None:
            return sample.get(key)
        if key in body and body.get(key) is not None:
            return body.get(key)
    return None


def sample_count_from_body(body: Any) -> int:
    if not isinstance(body, dict):
        return 1
    samples = body.get("samples")
    if isinstance(samples, list) and samples:
        return max(1, len(samples))
    return 1


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stm_timestamp_from_sample(sample: dict[str, Any]) -> float | None:
    for key in ("stm_extended_ms", "stm_ms", "t", "ts", "timestamp_ms"):
        value = _as_float(sample.get(key))
        if value is not None:
            return float(value)
    return None


def extract_stm_timestamps_ms(body: Any) -> list[float]:
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


def record_traffic(
    sample_count: int,
    *,
    stm_timestamps_ms: list[float] | None = None,
) -> None:
    global total_packets, total_samples, last_traffic_mono
    global last_stm_ms_seen, last_stm_advance_mono
    total_packets += 1
    last_traffic_mono = monotonic()
    if stm_timestamps_ms:
        newest = float(max(stm_timestamps_ms))
        prev = last_stm_ms_seen
        # STM 재시작/랩이면 시계가 크게 뒤로 간다 → 워터마크 리셋
        rebooted = prev is not None and newest + 1000.0 < float(prev)
        if prev is None or newest > float(prev) or rebooted:
            if rebooted:
                packet_times.clear()
            last_stm_ms_seen = newest
            last_stm_advance_mono = last_traffic_mono
        for stamp in stm_timestamps_ms:
            packet_times.append(float(stamp))
        total_samples += len(stm_timestamps_ms)
        return

    # STM 시계가 없으면 수신 시각만으로 live 유지
    last_stm_advance_mono = last_traffic_mono
    now_ms = monotonic() * 1000.0
    safe_count = max(1, sample_count)
    total_samples += safe_count
    for _ in range(safe_count):
        packet_times.append(now_ms)


def stream_is_live() -> bool:
    now = monotonic()
    if last_traffic_mono <= 0.0:
        return False
    if (now - last_traffic_mono) > LIVE_STALE_SEC:
        return False
    # STM 시계가 멈춘 채 같은 값이 반복 전송되면 죽은 스트림으로 본다.
    if last_stm_advance_mono > 0.0 and (now - last_stm_advance_mono) > LIVE_STALE_SEC:
        return False
    return True


def stream_age_sec() -> float:
    """live 판정에 쓰는 ‘얼마나 묵었는지’ (수신/STM 중 더 오래된 쪽)."""
    now = monotonic()
    ages: list[float] = []
    if last_traffic_mono > 0.0:
        ages.append(now - last_traffic_mono)
    if last_stm_advance_mono > 0.0:
        ages.append(now - last_stm_advance_mono)
    return max(ages) if ages else 1e9


def maybe_clear_stale_sensor_state() -> bool:
    """짧은 끊김은 직전 값 유지. 오래 끊긴 뒤에만 0으로 비운다."""
    if stream_is_live():
        return False
    clear_after = max(LIVE_STALE_SEC, LIVE_CLEAR_SEC)
    if stream_age_sec() < clear_after:
        return False
    clear_live_sensor_state()
    return True


def clear_live_sensor_state() -> None:
    """로깅 포워드가 끊기면 마지막 값이 화면에 남지 않도록 전부 내린다."""
    packet_times.clear()
    empty_motor = parse_motor_status(None)
    empty_bms = parse_bms_status({})
    sensor_state.update(
        {
            "stm_ms": 0.0,
            "FR": 0.0,
            "FL": 0.0,
            "RR": 0.0,
            "RL": 0.0,
            "x_g": 0.0,
            "y_g": 0.0,
            "z_g": 0.0,
            "ecu_temp": 0.0,
            "steering_angle": 0.0,
            "steering_speed": 0.0,
            "inv_temp_igt": 0.0,
            "inv_temp_motor": 0.0,
            "inv_motor_speed": 0.0,
            "vehicle_speed_kmh": 0.0,
            "inv_dc_current": 0.0,
            "inv_voltage": 0.0,
            "INV_shudder_torque": 0.0,
            "INV_torque_feedback": 0.0,
            "INV_torque_commanded": 0.0,
            "INV_id_feedback": 0.0,
            "INV_iq_feedback": 0.0,
            "INV_id_command": 0.0,
            "INV_iq_command": 0.0,
            "motor_precharge": False,
            "motor_main_contactor": False,
            "motor_inverter_mode": 0,
            "motor_inverter_mode_label": "Torque",
            "can1": 0,
            "can2": 0,
            "bms_charge": 0.0,
            "bms_voltage": 0.0,
            "bms_current": 0.0,
            "bms_ccl": 0.0,
            "bms_dcl": 0.0,
            "bms_temp_maxvalue": 0.0,
            "bms_capacity": 0.0,
            "bms_status": empty_bms,
            "bms_status_items": public_bms_status_items(empty_bms),
            "motor_status": empty_motor,
            "motor_status_items": public_motor_status_items(empty_motor),
            "vsm_state": "",
            "inv_state": "",
            "accel_x": 0.0,
            "accel_y": 0.0,
            "accel_z": 0.0,
            "core_temp": 0.0,
            "linear_fl": 0.0,
            "linear_fr": 0.0,
            "linear_rl": 0.0,
            "linear_rr": 0.0,
            "motor_rpm": 0.0,
            "device": "-",
            "updated_at": "",
            "live": False,
        }
    )


def hz_last(seconds: float) -> float:
    if not stream_is_live() or len(packet_times) < 2:
        return 0.0
    window_ms = max(1.0, float(seconds) * 1000.0)
    newest = packet_times[-1]
    oldest = newest
    count = 1
    for stamp in reversed(list(packet_times)[:-1]):
        if stamp > oldest:
            break
        if newest - stamp > window_ms:
            break
        oldest = stamp
        count += 1
    span_ms = newest - oldest
    if span_ms <= 0:
        return 0.0
    return (count - 1) * 1000.0 / span_ms


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
    inverter_motor = (
        _nested_dict(inverter.get("motor"))
        or _nested_dict(sample.get("motor"))
        or _nested_dict(body.get("motor"))
    )
    inverter_temp = _nested_dict(inverter.get("temperature"))
    inverter_current = _nested_dict(inverter.get("current"))
    inverter_voltage = _nested_dict(inverter.get("voltage"))
    inverter_torque = _nested_dict(inverter.get("torque"))
    inverter_idq_feedback = _nested_dict(inverter.get("feedback"))
    inverter_idq_command = _nested_dict(inverter.get("command"))
    can = _nested_dict(sample.get("can")) or _nested_dict(body.get("can"))
    bms = _nested_dict(sample.get("bms")) or _nested_dict(body.get("bms"))
    bms_temp = _nested_dict(bms.get("temperature"))

    sensor_state["device"] = device
    sensor_state["updated_at"] = str(payload.get("time") or "")
    sensor_state["live"] = True

    mapping = {
        "stm_ms": (
            _pick(sample, body, "stm_ms", "t", "ts", "timestamp_ms"),
        ),
        "FR": (
            _pick(sample, body, "FR", "fr", "linear_fr"),
            sample_linear.get("fr"),
            sample_linear.get("front_right"),
        ),
        "FL": (
            _pick(sample, body, "FL", "fl", "linear_fl"),
            sample_linear.get("fl"),
            sample_linear.get("front_left"),
        ),
        "RR": (
            _pick(sample, body, "RR", "rr", "linear_rr"),
            sample_linear.get("rr"),
            sample_linear.get("rear_right"),
        ),
        "RL": (
            _pick(sample, body, "RL", "rl", "linear_rl"),
            sample_linear.get("rl"),
            sample_linear.get("rear_left"),
        ),
        "x_g": (
            _pick(sample, body, "x_g", "accelX", "ax", "x"),
            sample_accel.get("x"),
            sample_accel.get("x_g"),
        ),
        "y_g": (
            _pick(sample, body, "y_g", "accelY", "ay", "y"),
            sample_accel.get("y"),
            sample_accel.get("y_g"),
        ),
        "z_g": (
            _pick(sample, body, "z_g", "accelZ", "az", "z"),
            sample_accel.get("z"),
            sample_accel.get("z_g"),
        ),
        # ECU: tp보다 ecu_temp 우선 (예전 버그는 tp만 보고 0으로 남김)
        "ecu_temp": (
            _pick(sample, body, "ecu_temp", "ECU_temp", "ecu"),
            sample.get("tp"),
            body.get("tp"),
        ),
        "steering_angle": (_pick(sample, body, "steering_angle"),),
        "steering_speed": (_pick(sample, body, "steering_speed"),),
        "inv_temp_igt": (
            _pick(sample, body, "inv_temp_igt", "INV_TEMP_IGT"),
            _nested_dict(_nested_dict(inverter_temp.get("igbt")).get("max")).get("temperature"),
        ),
        "inv_temp_motor": (
            _pick(sample, body, "inv_temp_motor", "INV_TEMP_motor"),
            inverter_temp.get("motor"),
        ),
        "inv_motor_speed": (
            _pick(
                sample,
                body,
                "inv_motor_speed",
                "inv_moter_speed",
                "INV_moter_speed",
                "INV_motor_speed",
                "motor_rpm",
            ),
            inverter_motor.get("speed"),
            inverter_motor.get("rpm"),
        ),
        "inv_dc_current": (
            _pick(sample, body, "inv_dc_current", "inv_dc_currnet", "INV_dc_currnet"),
            inverter_current.get("dc_bus"),
        ),
        "inv_voltage": (
            _pick(sample, body, "inv_voltage", "INV_voltage"),
            inverter_voltage.get("dc_bus"),
        ),
        "INV_shudder_torque": (
            _pick(sample, body, "INV_shudder_torque", "inv_shudder_torque"),
            inverter_torque.get("shudder"),
        ),
        "INV_torque_feedback": (
            _pick(sample, body, "INV_torque_feedback", "inv_torque_feedback"),
            inverter_torque.get("feedback"),
        ),
        "INV_torque_commanded": (
            _pick(sample, body, "INV_torque_commanded", "inv_torque_commanded"),
            inverter_torque.get("commanded"),
        ),
        "INV_id_feedback": (
            _pick(sample, body, "INV_id_feedback", "inv_id_feedback"),
            inverter_idq_feedback.get("id"),
        ),
        "INV_iq_feedback": (
            _pick(sample, body, "INV_iq_feedback", "inv_iq_feedback"),
            inverter_idq_feedback.get("iq"),
        ),
        "INV_id_command": (
            _pick(sample, body, "INV_id_command", "inv_id_command"),
            inverter_idq_command.get("id"),
        ),
        "INV_iq_command": (
            _pick(sample, body, "INV_iq_command", "inv_iq_command"),
            inverter_idq_command.get("iq"),
        ),
        "can1": (
            _pick(sample, body, "can1"),
            can.get("can1"),
        ),
        "can2": (
            _pick(sample, body, "can2"),
            can.get("can2"),
        ),
        "bms_charge": (_pick(sample, body, "bms_charge", "BMS_charge"), bms.get("charge")),
        "bms_voltage": (_pick(sample, body, "bms_voltage", "BMS_voltage"), bms.get("voltage")),
        "bms_current": (_pick(sample, body, "bms_current", "BMS_current"), bms.get("current")),
        "bms_ccl": (_pick(sample, body, "bms_ccl", "BMS_ccl"), bms.get("ccl")),
        "bms_dcl": (_pick(sample, body, "bms_dcl", "BMS_dcl"), bms.get("dcl")),
        "bms_temp_maxvalue": (
            _pick(sample, body, "bms_temp_maxvalue", "BMS_TEMP_maxvalue"),
            _nested_dict(bms_temp.get("max")).get("value"),
            bms_temp.get("max"),
            bms_temp.get("maxvalue"),
        ),
        "bms_capacity": (_pick(sample, body, "bms_capacity", "BMS_capacity"), bms.get("capacity")),
    }
    for key, candidates in mapping.items():
        _set_metric(key, *candidates)

    for can_key in ("can1", "can2"):
        raw = sensor_state.get(can_key)
        try:
            if isinstance(raw, str):
                sensor_state[can_key] = 1 if raw.strip().lower() in {"1", "true", "yes", "on"} else 0
            elif raw is None:
                sensor_state[can_key] = 0
            else:
                sensor_state[can_key] = 1 if int(raw) else 0
        except Exception:
            sensor_state[can_key] = 0

    # motor flags / mode (flat Pi keys + nested)
    # motor_inverter_mode: 0=Torque, 1=Speed (0x0AA Byte4 Bit0)
    motor = parse_motor_status(inverter, sample)
    for flat_key, status_key in (
        ("motor_precharge", "precharge"),
        ("motor_main_contactor", "main_contactor"),
    ):
        raw = _pick(sample, body, flat_key, status_key)
        if raw is not None:
            try:
                if isinstance(raw, str):
                    motor[status_key] = raw.strip().lower() in {"1", "true", "yes", "on"}
                else:
                    motor[status_key] = bool(int(raw)) if isinstance(raw, (int, float)) else bool(raw)
            except Exception:
                pass

    mode_raw = _pick(sample, body, "motor_inverter_mode", "inverter_mode", "inv_mode")
    mode_code = 0
    if mode_raw is not None:
        try:
            if isinstance(mode_raw, str):
                lowered = mode_raw.strip().lower()
                if lowered in {"1", "speed", "spd"}:
                    mode_code = 1
                elif lowered in {"0", "torque", "trq"}:
                    mode_code = 0
                else:
                    mode_code = 1 if float(lowered) else 0
            else:
                mode_code = 1 if int(mode_raw) else 0
        except Exception:
            mode_code = 0
    motor["inverter_mode"] = mode_code

    sensor_state["motor_precharge"] = bool(motor.get("precharge"))
    sensor_state["motor_main_contactor"] = bool(motor.get("main_contactor"))
    sensor_state["motor_inverter_mode"] = mode_code
    sensor_state["motor_inverter_mode_label"] = "Speed" if mode_code == 1 else "Torque"
    sensor_state["motor_status"] = motor
    sensor_state["motor_status_items"] = public_motor_status_items(motor)

    parsed_bms = parse_bms_status(bms)
    sensor_state["bms_status"] = parsed_bms
    sensor_state["bms_status_items"] = public_bms_status_items(parsed_bms)
    sensor_state["vsm_state"] = parse_vsm_state(inverter, sample)
    sensor_state["inv_state"] = parse_inv_state(inverter, sample)

    # 별칭 동기화
    sensor_state["accel_x"] = float(sensor_state.get("x_g") or 0)
    sensor_state["accel_y"] = float(sensor_state.get("y_g") or 0)
    sensor_state["accel_z"] = float(sensor_state.get("z_g") or 0)
    sensor_state["core_temp"] = float(sensor_state.get("ecu_temp") or 0)
    sensor_state["linear_fl"] = float(sensor_state.get("FL") or 0)
    sensor_state["linear_fr"] = float(sensor_state.get("FR") or 0)
    sensor_state["linear_rl"] = float(sensor_state.get("RL") or 0)
    sensor_state["linear_rr"] = float(sensor_state.get("RR") or 0)
    sensor_state["motor_rpm"] = float(sensor_state.get("inv_motor_speed") or 0)
    speed = motor_rpm_to_speed_kmh(sensor_state.get("inv_motor_speed"))
    sensor_state["vehicle_speed_kmh"] = float(speed if speed is not None else 0.0)


@app.get("/")
def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="realtime.html",
        context={"request": request, **nav_context("realtime", request)},
    )


@app.post("/api/live/ingest")
async def live_ingest(request: Request) -> dict[str, Any]:
    global last_traffic_mono, last_stm_ms_seen, last_stm_advance_mono, last_ui_publish_mono
    body = await request.json()
    sample_count = sample_count_from_body(body)
    stm_timestamps_ms = extract_stm_timestamps_ms(body)

    # live keepalive 는 매 수신마다 (throttle 과 무관)
    now = monotonic()
    last_traffic_mono = now
    if stm_timestamps_ms:
        newest = float(max(stm_timestamps_ms))
        prev = last_stm_ms_seen
        rebooted = prev is not None and newest + 1000.0 < float(prev)
        if prev is None or newest > float(prev) or rebooted:
            last_stm_ms_seen = newest
            last_stm_advance_mono = now
    else:
        last_stm_advance_mono = now

    if (now - last_ui_publish_mono) < UI_PUBLISH_INTERVAL_SEC:
        return {"ok": True, "samples": sample_count, "published": False}
    last_ui_publish_mono = now

    # UI 틱: 최신 샘플 1개만 Hz/화면에 반영 → 약 REALTIME_UI_HZ
    ui_stamps = [float(max(stm_timestamps_ms))] if stm_timestamps_ms else None
    record_traffic(1, stm_timestamps_ms=ui_stamps)

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
    return {"ok": True, "samples": sample_count, "published": True}


@app.get("/api/live/snapshot")
def live_snapshot() -> dict[str, Any]:
    live = stream_is_live()
    cleared = maybe_clear_stale_sensor_state()
    return {
        "ok": True,
        "live": live,
        "holding": (not live) and (not cleared),
        **sensor_state,
    }


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    live = stream_is_live()
    cleared = maybe_clear_stale_sensor_state()
    return {
        "total_packets": total_packets,
        "total_samples": total_samples,
        "hz_1s": round(hz_last(1.0), 1) if live else 0.0,
        "hz_5s_avg": round(hz_last(5.0), 1) if live else 0.0,
        "hz_basis": "ui_5hz",
        "live": live,
        "holding": (not live) and (not cleared),
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
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[offset] status fetch failed: {exc} url={LOGGER_OFFSET_URL}")
    return {"ok": False, "devices": {}}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8011"))
    uvicorn.run("realtime_server:app", host="0.0.0.0", port=port, reload=False)
