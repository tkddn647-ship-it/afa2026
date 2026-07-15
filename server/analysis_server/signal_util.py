"""Lightweight signal helpers for CSV post-processing (no numpy)."""

from __future__ import annotations

import math
from typing import Iterable


def num(value: float | None, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, float) and math.isnan(value):
        return default
    return float(value)


def moving_average(values: list[float | None], window: int = 10) -> list[float]:
    if window < 1:
        window = 1
    n = len(values)
    out: list[float] = []
    half = window // 2
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        chunk = [num(values[j]) for j in range(lo, hi)]
        out.append(sum(chunk) / len(chunk))
    return out


def gradient(values: list[float | None], dt: float) -> list[float]:
    n = len(values)
    if n == 0:
        return []
    if dt <= 0:
        dt = 0.005
    out = [0.0] * n
    for i in range(1, n):
        out[i] = (num(values[i]) - num(values[i - 1])) / dt
    return out


def local_maxima(
    values: list[float],
    *,
    min_value: float,
    min_spacing: int,
) -> list[int]:
    """Return indices of local maxima with minimum index spacing."""
    n = len(values)
    if n < 3:
        return []
    candidates: list[int] = []
    for i in range(1, n - 1):
        if values[i] < min_value:
            continue
        if values[i] >= values[i - 1] and values[i] >= values[i + 1]:
            if values[i] > values[i - 1] or values[i] > values[i + 1]:
                candidates.append(i)
    if not candidates:
        return []
    candidates.sort(key=lambda idx: values[idx], reverse=True)
    picked: list[int] = []
    for idx in candidates:
        if all(abs(idx - kept) >= min_spacing for kept in picked):
            picked.append(idx)
    picked.sort()
    return picked


def percentile(values: Iterable[float], pct: float) -> float:
    arr = sorted(float(v) for v in values)
    if not arr:
        return 0.0
    pct = max(0.0, min(100.0, pct))
    k = (len(arr) - 1) * (pct / 100.0)
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return arr[lo]
    return arr[lo] + (arr[hi] - arr[lo]) * (k - lo)


def decimate_bucket_average(
    times: list[float],
    series: dict[str, list[float | None]],
    *,
    target_hz: float = 20.0,
) -> tuple[list[float], dict[str, list[float]]]:
    n = len(times)
    if n == 0:
        return [], {}
    if n == 1:
        out = {k: [num(v[0])] for k, v in series.items()}
        return [times[0]], out

    dt = (times[-1] - times[0]) / max(1, n - 1)
    step = max(1, int(round((1.0 / max(1.0, target_hz)) / max(dt, 1e-6))))
    out_times: list[float] = []
    out_series: dict[str, list[float]] = {k: [] for k in series}
    for i in range(0, n, step):
        j = min(n, i + step)
        mid = i + (j - i) // 2
        out_times.append(times[mid])
        for key, vals in series.items():
            chunk = [num(vals[k]) for k in range(i, j)]
            out_series[key].append(sum(chunk) / len(chunk))
    return out_times, out_series


def resample_values(values: list[float | None], points: int) -> list[float]:
    n = len(values)
    if n == 0:
        return []
    if points < 2:
        return [num(values[0])]
    if n == 1:
        return [num(values[0])] * points
    out: list[float] = []
    for i in range(points):
        pos = i * (n - 1) / (points - 1)
        lo = int(math.floor(pos))
        hi = min(n - 1, lo + 1)
        frac = pos - lo
        out.append(num(values[lo]) * (1.0 - frac) + num(values[hi]) * frac)
    return out
