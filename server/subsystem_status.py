from __future__ import annotations

from typing import Any

BMS_STATUS_DEFS: tuple[dict[str, str], ...] = (
    {"key": "voltage_failsafe", "code": "V", "label": "전압 페일세이프"},
    {"key": "current_failsafe", "code": "A", "label": "전류 페일세이프"},
    {"key": "relay_failsafe", "code": "R", "label": "릴레이 페일세이프"},
    {"key": "cell_balancing", "code": "B", "label": "셀 벨런싱"},
    {"key": "interlock_failsafe", "code": "I", "label": "인터락 페일세이프"},
    {"key": "thermistor_error", "code": "T", "label": "써미스터 오류"},
    {"key": "input_power_failsafe", "code": "P", "label": "입력전원 페일세이프"},
)

MOTOR_STATUS_DEFS: tuple[dict[str, str], ...] = (
    {"key": "precharge", "code": "P", "label": "프리차지"},
    {"key": "main_contactor", "code": "A", "label": "메인 컨택터"},
    {"key": "inverter_mode", "code": "X", "label": "인버터 모드"},
)

BMS_STATUS_ALIASES: dict[str, tuple[str, ...]] = {
    "voltage_failsafe": ("voltage_failsafe", "v_failsafe", "voltage_fail", "V"),
    "current_failsafe": ("current_failsafe", "a_failsafe", "current_fail", "A"),
    "relay_failsafe": ("relay_failsafe", "r_failsafe", "relay_fail", "R"),
    "cell_balancing": ("cell_balancing", "balancing", "balance", "B"),
    "interlock_failsafe": ("interlock_failsafe", "i_failsafe", "interlock_fail", "I"),
    "thermistor_error": ("thermistor_error", "thermistor", "temp_error", "T"),
    "input_power_failsafe": ("input_power_failsafe", "power_failsafe", "input_fail", "P"),
}

MOTOR_STATUS_ALIASES: dict[str, tuple[str, ...]] = {
    "precharge": ("precharge", "pre_charge", "P"),
    "main_contactor": ("main_contactor", "contactor", "main_relay", "A"),
    "inverter_mode": ("inverter_mode", "mode", "inv_mode", "X"),
}


def _empty_bms_status() -> dict[str, Any]:
    return {item["key"]: False for item in BMS_STATUS_DEFS}


def _empty_motor_status() -> dict[str, Any]:
    return {
        "precharge": False,
        "main_contactor": False,
        "inverter_mode": "",
    }


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "active", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "inactive", "disabled"}:
            return False
    return None


def _pick_value(source: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    for alias in aliases:
        if alias in source:
            return source.get(alias)
    return None


def _pick_text(*candidates: Any) -> str:
    for candidate in candidates:
        if candidate is None:
            continue
        text = str(candidate).strip()
        if text:
            return text
    return ""


def parse_bms_status(bms: dict[str, Any] | None) -> dict[str, Any]:
    status = dict(_empty_bms_status())
    if not isinstance(bms, dict):
        return status

    nested = {}
    for key in ("status", "failsafe", "flags"):
        block = bms.get(key)
        if isinstance(block, dict):
            nested.update(block)

    for item in BMS_STATUS_DEFS:
        key = item["key"]
        raw = _pick_value(nested, BMS_STATUS_ALIASES[key])
        if raw is None:
            raw = _pick_value(bms, BMS_STATUS_ALIASES[key])
        coerced = _coerce_bool(raw)
        if coerced is not None:
            status[key] = coerced

    return status


def parse_motor_status(inverter: dict[str, Any] | None, sample: dict[str, Any] | None = None) -> dict[str, Any]:
    status: dict[str, Any] = dict(_empty_motor_status())
    sample = sample if isinstance(sample, dict) else {}
    inverter = inverter if isinstance(inverter, dict) else {}

    nested = {}
    for key in ("status", "state", "flags"):
        block = inverter.get(key)
        if isinstance(block, dict):
            nested.update(block)
    motor_block = inverter.get("motor")
    if isinstance(motor_block, dict):
        nested.update({k: v for k, v in motor_block.items() if k in {"precharge", "main_contactor", "contactor", "mode"}})

    for item in MOTOR_STATUS_DEFS:
        key = item["key"]
        if key == "inverter_mode":
            continue
        raw = _pick_value(nested, MOTOR_STATUS_ALIASES[key])
        if raw is None:
            raw = _pick_value(inverter, MOTOR_STATUS_ALIASES[key])
        if raw is None:
            raw = _pick_value(sample, MOTOR_STATUS_ALIASES[key])
        coerced = _coerce_bool(raw)
        if coerced is not None:
            status[key] = coerced

    mode = _pick_text(
        _pick_value(nested, MOTOR_STATUS_ALIASES["inverter_mode"]),
        _pick_value(inverter, MOTOR_STATUS_ALIASES["inverter_mode"]),
        inverter.get("mode_name"),
        sample.get("inverter_mode"),
    )
    if mode:
        status["inverter_mode"] = mode

    return status


def parse_vsm_state(inverter: dict[str, Any] | None, sample: dict[str, Any] | None = None) -> str:
    sample = sample if isinstance(sample, dict) else {}
    inverter = inverter if isinstance(inverter, dict) else {}
    vsm = inverter.get("vsm")
    if isinstance(vsm, dict):
        return _pick_text(vsm.get("state"), vsm.get("status"), vsm.get("mode"), vsm.get("name"))
    return _pick_text(vsm, inverter.get("vsm_state"), sample.get("vsm"), sample.get("VSM"))


def parse_inv_state(inverter: dict[str, Any] | None, sample: dict[str, Any] | None = None) -> str:
    sample = sample if isinstance(sample, dict) else {}
    inverter = inverter if isinstance(inverter, dict) else {}
    return _pick_text(
        inverter.get("state"),
        inverter.get("status"),
        inverter.get("mode_name"),
        sample.get("INV"),
        sample.get("inv_state"),
    )


def public_bms_status_items(status: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in BMS_STATUS_DEFS:
        key = item["key"]
        active = bool(status.get(key))
        items.append(
            {
                "key": key,
                "code": item["code"],
                "label": item["label"],
                "active": active,
                "kind": "info" if key == "cell_balancing" else "alert",
            }
        )
    return items


def public_motor_status_items(status: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in MOTOR_STATUS_DEFS:
        key = item["key"]
        value = status.get(key)
        if key == "inverter_mode":
            text = _pick_text(value)
            items.append(
                {
                    "key": key,
                    "code": item["code"],
                    "label": item["label"],
                    "active": bool(text),
                    "text": text or "N/A",
                    "kind": "mode",
                }
            )
            continue
        items.append(
            {
                "key": key,
                "code": item["code"],
                "label": item["label"],
                "active": bool(value),
                "kind": "state",
            }
        )
    return items
