#!/usr/bin/env python3
"""Estimate steering_angle from steering_speed (+ y_g / linear roll) when angle is stuck."""

from __future__ import annotations

import argparse
import gzip
import math
import shutil
from pathlib import Path
from typing import Any


def _as_float(value: str) -> float | None:
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _fmt(value: float, template: str) -> str:
    if template and "." in template:
        decimals = len(template.split(".", 1)[1])
        return f"{value:.{decimals}f}"
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.2f}"


def angle_looks_broken(angles: list[float | None], speeds: list[float | None]) -> bool:
    valid = [round(a, 1) for a in angles if a is not None]
    if not valid:
        return True
    unique = len(set(valid))
    changes = 0
    prev = None
    for a in angles:
        if a is None:
            continue
        if prev is not None and abs(a - prev) > 0.05:
            changes += 1
        prev = a
    spd_active = sum(1 for s in speeds if s is not None and abs(s) > 1.0)
    n = max(1, len(angles))
    if unique <= 30 and changes < max(50, int(0.01 * n)) and spd_active > max(100, int(0.01 * n)):
        return True
    # dominant value covers almost everything
    from collections import Counter

    top_n = Counter(valid).most_common(1)[0][1]
    # e.g. stuck at 1.0 for ~70%+ while rate still moves
    if top_n / len(valid) >= 0.65 and spd_active > 200:
        return True
    return False


def estimate_angles(rows: list[dict[str, Any]]) -> list[float]:
    """Burst-locked sign from y_g + roll; integrate |steering_speed|; soft-return to 0."""
    rolls: list[float] = []
    for r in rows[:1000]:
        fl, fr, rl, rr = r.get("FL"), r.get("FR"), r.get("RL"), r.get("RR")
        if None not in (fl, fr, rl, rr):
            rolls.append((fr + rr) - (fl + rl))
    base = sum(rolls) / len(rolls) if rolls else 0.0

    out: list[float] = []
    angle = 0.0
    sign = 1.0
    y_f = 0.0
    roll_f = 0.0
    prev_stm: float | None = None
    active = False
    idle_t = 0.0
    spd_max = 160.0
    center_tau = 1.2
    alpha = 0.1

    for r in rows:
        stm = r.get("stm_ms")
        if stm is None or prev_stm is None:
            if stm is not None:
                prev_stm = float(stm)
            out.append(angle)
            if r.get("y_g") is not None:
                y_f = float(r["y_g"])
            continue

        dt = (float(stm) - float(prev_stm)) / 1000.0
        prev_stm = float(stm)
        if dt <= 0:
            dt = 0.01
        if dt > 0.2:
            dt = 0.05

        y = 0.0 if r.get("y_g") is None else float(r["y_g"])
        y_f = (1 - alpha) * y_f + alpha * y
        fl, fr, rl, rr = r.get("FL"), r.get("FR"), r.get("RL"), r.get("RR")
        if None not in (fl, fr, rl, rr):
            roll = ((fr + rr) - (fl + rl)) - base
            roll_f = (1 - alpha) * roll_f + alpha * roll

        raw = float(r["steering_speed"] or 0.0)
        if raw < -0.5:
            mag = min(-raw, spd_max)
            signed = True
            burst_sign = -1.0
        else:
            mag = min(abs(raw), spd_max)
            signed = False
            burst_sign = None

        score = y_f + 0.02 * roll_f
        if mag >= 2.0:
            if not active:
                if signed and burst_sign is not None:
                    sign = burst_sign
                elif abs(score) >= 0.02:
                    sign = 1.0 if score >= 0 else -1.0
                active = True
            idle_t = 0.0
            angle += sign * mag * dt
        else:
            idle_t += dt
            if idle_t > 0.2:
                active = False
                angle *= math.exp(-dt / center_tau)
        angle = max(-120.0, min(120.0, angle))
        out.append(angle)
    return out


def process_file(path: Path, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
    opener = gzip.open if path.suffix == ".gz" or path.name.endswith(".csv.gz") else open
    with opener(path, "rt") as fin:  # type: ignore[arg-type]
        header = fin.readline()
        cols = header.rstrip("\n").split(",")
        idx = {c: i for i, c in enumerate(cols)}
        required = ("steering_angle", "steering_speed", "stm_ms", "y_g", "RL", "RR", "FL", "FR")
        missing = [c for c in required if c not in idx]
        if missing:
            return {"ok": False, "path": str(path), "error": f"missing columns: {missing}"}

        raw_lines: list[str] = []
        rows: list[dict[str, Any]] = []
        angles: list[float | None] = []
        speeds: list[float | None] = []
        templates: list[str] = []
        for line in fin:
            raw_lines.append(line)
            parts = line.rstrip("\n").split(",")
            def get(name: str) -> float | None:
                i = idx[name]
                if i >= len(parts):
                    return None
                return _as_float(parts[i])

            ang = get("steering_angle")
            spd = get("steering_speed")
            angles.append(ang)
            speeds.append(spd)
            templates.append(parts[idx["steering_angle"]] if idx["steering_angle"] < len(parts) else "")
            rows.append(
                {
                    "steering_angle": ang,
                    "steering_speed": spd,
                    "stm_ms": get("stm_ms"),
                    "y_g": get("y_g"),
                    "RL": get("RL"),
                    "RR": get("RR"),
                    "FL": get("FL"),
                    "FR": get("FR"),
                    "vehicle_speed_kmh": get("vehicle_speed_kmh") if "vehicle_speed_kmh" in idx else None,
                }
            )

    broken = angle_looks_broken(angles, speeds)
    if not broken and not force:
        return {"ok": True, "path": str(path), "skipped": True, "reason": "angle looks usable"}

    estimated = estimate_angles(rows)
    if dry_run:
        return {
            "ok": True,
            "path": str(path),
            "dry_run": True,
            "broken": broken,
            "est_min": min(estimated),
            "est_max": max(estimated),
        }

    bak = path.with_suffix(path.suffix + ".bak_steer_est")
    if not bak.exists():
        shutil.copy2(path, bak)

    tmp = path.with_name(path.name + ".tmp")
    gz = path.name.endswith(".gz")
    fout_cm = gzip.open(tmp, "wt", compresslevel=6) if gz else open(tmp, "w", encoding="utf-8")
    with fout_cm as fout:
        # keep original angle column name; analysis UI uses steering_angle
        # Replace steering_angle in place; drop any previous steering_angle_raw column.
        cols_out = [c for c in cols if c != "steering_angle_raw"]
        fout.write(",".join(cols_out) + "\n")

        ai = idx["steering_angle"]
        raw_i = idx.get("steering_angle_raw")
        for line, est, tmpl in zip(raw_lines, estimated, templates):
            parts = line.rstrip("\n").split(",")
            while len(parts) < len(cols):
                parts.append("")
            parts[ai] = _fmt(est, tmpl or "0.0")
            if raw_i is not None:
                parts = [p for i, p in enumerate(parts) if i != raw_i]
            else:
                parts = parts[: len(cols_out)]
            fout.write(",".join(parts) + "\n")

    tmp.replace(path)
    return {
        "ok": True,
        "path": str(path),
        "broken": broken,
        "rows": len(estimated),
        "est_min": min(estimated),
        "est_max": max(estimated),
        "backup": str(bak),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    for path in args.paths:
        result = process_file(path, force=args.force, dry_run=args.dry_run)
        print(result)


if __name__ == "__main__":
    main()
