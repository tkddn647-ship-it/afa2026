from __future__ import annotations

from typing import Any

ACCEL_Z_SENSOR_PATHS = {"car.accel.z"}
ACCEL_Z_REFERENCE_G = 1.0

SENSOR_OFFSET_LABELS: dict[str, str] = {
    "car.linear.front_left": "FL Linear",
    "car.linear.front_right": "FR Linear",
    "car.linear.rear_left": "RL Linear",
    "car.linear.rear_right": "RR Linear",
    "car.accel.x": "가속 X",
    "car.accel.y": "가속 Y",
    "car.accel.z": "가속 Z",
}

SENSOR_OFFSET_UNITS: dict[str, str] = {
    "car.linear.front_left": "mm",
    "car.linear.front_right": "mm",
    "car.linear.rear_left": "mm",
    "car.linear.rear_right": "mm",
    "car.accel.x": "g",
    "car.accel.y": "g",
    "car.accel.z": "g",
}

LINEAR_SENSOR_PATHS: dict[str, str] = {
    "front_left": "car.linear.front_left",
    "front_right": "car.linear.front_right",
    "rear_left": "car.linear.rear_left",
    "rear_right": "car.linear.rear_right",
    "fl": "car.linear.front_left",
    "fr": "car.linear.front_right",
    "rl": "car.linear.rear_left",
    "rr": "car.linear.rear_right",
}

FLAT_LINEAR_KEYS: dict[str, str] = {
    "linear_fl": "car.linear.front_left",
    "linear_fr": "car.linear.front_right",
    "linear_rl": "car.linear.rear_left",
    "linear_rr": "car.linear.rear_right",
}

ACCEL_SENSOR_PATHS: dict[str, str] = {
    "x": "car.accel.x",
    "y": "car.accel.y",
    "z": "car.accel.z",
    "ax": "car.accel.x",
    "ay": "car.accel.y",
    "az": "car.accel.z",
}

FLAT_ACCEL_KEYS: dict[str, str] = {
    "accelX": "car.accel.x",
    "accelY": "car.accel.y",
    "accelZ": "car.accel.z",
}

ACCEL_CONTAINER_KEYS = ("accel", "accelerometer", "acceleration")


def sensor_offset_label(path: str) -> str:
    return SENSOR_OFFSET_LABELS.get(path, path)


def sensor_offset_unit(path: str) -> str:
    return SENSOR_OFFSET_UNITS.get(path, "")


def format_offset_display(path: str, value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)

    unit = sensor_offset_unit(path)
    if path in ACCEL_Z_SENSOR_PATHS:
        return f"{number:+.3f}{unit} (정지 1G 기준)"
    if unit == "g":
        return f"{number:+.3f}{unit}"
    if unit:
        return f"{number:+.2f} {unit}"
    return f"{number:+.4f}"


def public_offset_status(device_offsets: dict[str, dict[str, float]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for device in sorted(device_offsets):
        offsets = device_offsets.get(device) or {}
        if not isinstance(offsets, dict):
            continue
        for path in sorted(offsets):
            value = offsets.get(path)
            if value is None:
                continue
            items.append(
                {
                    "device": device,
                    "path": path,
                    "label": sensor_offset_label(path),
                    "unit": sensor_offset_unit(path),
                    "value": value,
                    "display": format_offset_display(path, value),
                }
            )

    return {
        "offset_device_count": len(device_offsets),
        "offset_sensor_count": len(items),
        "offsets": device_offsets,
        "items": items,
    }


def as_float(value: Any) -> float | None:
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


def raw_value_to_offset(sensor_path: str, raw_value: float) -> float:
    if sensor_path in ACCEL_Z_SENSOR_PATHS:
        return round(raw_value - ACCEL_Z_REFERENCE_G, 4)
    return raw_value


def snapshot_to_offsets(snapshot: dict[str, float]) -> dict[str, float]:
    return {
        path: raw_value_to_offset(path, value)
        for path, value in snapshot.items()
    }


def collect_parsed_nodes(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = [parsed]

    car = parsed.get("car")
    if isinstance(car, dict):
        nodes.append(car)

    samples = parsed.get("samples")
    if isinstance(samples, list):
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            nodes.append(sample)
            sample_car = sample.get("car")
            if isinstance(sample_car, dict):
                nodes.append(sample_car)

    return nodes


def iter_linear_targets(node: dict[str, Any]) -> list[tuple[dict[str, Any], str, str]]:
    targets: list[tuple[dict[str, Any], str, str]] = []

    linear = node.get("linear")
    if isinstance(linear, dict):
        for key, path in LINEAR_SENSOR_PATHS.items():
            if key in linear:
                targets.append((linear, key, path))

    for flat_key, path in FLAT_LINEAR_KEYS.items():
        if flat_key in node:
            targets.append((node, flat_key, path))

    car = node.get("car")
    if isinstance(car, dict):
        car_linear = car.get("linear")
        if isinstance(car_linear, dict):
            for key, path in LINEAR_SENSOR_PATHS.items():
                if key in car_linear:
                    targets.append((car_linear, key, path))

    return targets


def iter_accel_targets(node: dict[str, Any]) -> list[tuple[dict[str, Any], str, str]]:
    targets: list[tuple[dict[str, Any], str, str]] = []

    for accel_key in ACCEL_CONTAINER_KEYS:
        accel = node.get(accel_key)
        if isinstance(accel, dict):
            for key, path in ACCEL_SENSOR_PATHS.items():
                if key in accel:
                    targets.append((accel, key, path))

    for flat_key, path in FLAT_ACCEL_KEYS.items():
        if flat_key in node:
            targets.append((node, flat_key, path))

    car = node.get("car")
    if isinstance(car, dict):
        for accel_key in ("accel2", *ACCEL_CONTAINER_KEYS):
            accel = car.get(accel_key)
            if not isinstance(accel, dict):
                continue
            alias = {
                "x": "x",
                "y": "y",
                "z": "z",
                "accel2_x": "x",
                "accel2_y": "y",
                "accel2_z": "z",
            }
            for raw_key, normalized in alias.items():
                if raw_key not in accel:
                    continue
                sensor_path = ACCEL_SENSOR_PATHS.get(normalized)
                if sensor_path:
                    targets.append((accel, raw_key, sensor_path))

    return targets


def extract_offset_candidates(parsed: dict[str, Any]) -> dict[str, float]:
    snapshot: dict[str, float] = {}

    for node in reversed(collect_parsed_nodes(parsed)):
        for container, key, path in iter_linear_targets(node):
            value = as_float(container.get(key))
            if value is not None:
                snapshot[path] = value

        for container, key, path in iter_accel_targets(node):
            value = as_float(container.get(key))
            if value is not None:
                snapshot[path] = value

    path = parsed.get("path")
    path_value = as_float(parsed.get("value"))
    if isinstance(path, str) and path_value is not None:
        snapshot[path] = path_value

    updates = parsed.get("sensor_updates")
    if isinstance(updates, list):
        for item in updates:
            if not isinstance(item, dict):
                continue
            update_path = item.get("path")
            value = as_float(item.get("value"))
            if isinstance(update_path, str) and value is not None:
                snapshot[update_path] = value

    return snapshot


def apply_offsets_to_parsed(parsed: dict[str, Any], offsets: dict[str, float]) -> bool:
    if not offsets:
        return False

    changed = False

    for node in collect_parsed_nodes(parsed):
        for container, key, path in iter_linear_targets(node):
            value = as_float(container.get(key))
            if value is None:
                continue
            offset = offsets.get(path)
            if offset is None:
                continue
            container[key] = round(value - offset, 4)
            changed = True

        for container, key, path in iter_accel_targets(node):
            value = as_float(container.get(key))
            if value is None:
                continue
            offset = offsets.get(path)
            if offset is None:
                continue
            container[key] = round(value - offset, 4)
            changed = True

    path = parsed.get("path")
    value = as_float(parsed.get("value"))
    if isinstance(path, str) and value is not None and path in offsets:
        parsed["value"] = round(value - offsets[path], 4)
        changed = True

    updates = parsed.get("sensor_updates")
    if isinstance(updates, list):
        for item in updates:
            if not isinstance(item, dict):
                continue
            update_path = item.get("path")
            update_value = as_float(item.get("value"))
            if not isinstance(update_path, str) or update_value is None:
                continue
            if update_path not in offsets:
                continue
            item["value"] = round(update_value - offsets[update_path], 4)
            changed = True

    return changed
