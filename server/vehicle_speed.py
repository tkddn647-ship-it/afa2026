"""Motor RPM → vehicle speed (km/h).

타이어 외경 460 mm, 감속비 3.9 기준:
  speed_kmh = motor_rpm * 0.0222328
"""

from __future__ import annotations

from typing import Any

TIRE_OUTER_DIAMETER_MM = 460.0
REDUCTION_RATIO = 3.9
# π * D_m * 3.6 / (ratio * 60) ≈ 0.0222328
MOTOR_RPM_TO_KMH = 0.0222328


def motor_rpm_to_speed_kmh(rpm: Any) -> float | None:
    try:
        if isinstance(rpm, bool):
            return None
        value = float(rpm)
    except (TypeError, ValueError):
        return None
    if not (value == value):  # NaN
        return None
    return round(value * MOTOR_RPM_TO_KMH, 3)
