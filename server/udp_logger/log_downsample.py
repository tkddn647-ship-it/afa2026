"""STM 타임스탬프 기준 로그 다운샘플 (app.py 에서 분리한 순수 함수).

기존 구현은 직전에 저장한 행보다 `min_interval_ms` 미만 뒤에 온 행을 모두 버렸다.
펌웨어 샘플은 10 ms 간격이지만 한 샘플이 1 ms 늦으면(간격 11 ms) 그다음 샘플은
9 ms 뒤에 오고, 이 정상 샘플이 "너무 빠른 행"으로 판정되어 버려졌다.
2026-07~08 주행 로그에서 200 ms 마다 1개씩(약 5%) 빠진 원인이다.

수정: 간격 판정에 지터 허용치를 둔다. 기본값은 목표 간격의 20% (100 Hz 에서 2 ms).
200 Hz 수신을 100 Hz 로 줄이는 원래 목적은 그대로 유지된다 (5 ms 간격은 여전히 버림).
"""

from __future__ import annotations

from typing import Any, Callable

RESET_BACKWARD_MS = 1000.0


def default_jitter_tolerance_ms(min_interval_ms: float) -> float:
    return 0.2 * max(0.0, float(min_interval_ms))


def downsample_rows(
    rows: list[dict[str, Any]],
    last_stm_ms: float | None,
    *,
    min_interval_ms: float,
    jitter_tolerance_ms: float | None = None,
    get_stamp: Callable[[dict[str, Any]], float | None],
) -> tuple[list[dict[str, Any]], float | None]:
    """Return (kept_rows, new_last_stm_ms).

    - 시계가 없는 행은 그대로 둔다 (유실보다 중복이 낫다).
    - 시계가 크게 뒤로 가면(STM 재부팅/랩) 워터마크를 리셋한다.
    - 직전 저장 행과의 간격이 `min_interval_ms - jitter_tolerance_ms` 미만이면 버린다.
    """
    min_interval = max(0.0, float(min_interval_ms))
    if not rows or min_interval <= 0:
        return rows, last_stm_ms
    if jitter_tolerance_ms is None:
        jitter_tolerance_ms = default_jitter_tolerance_ms(min_interval)
    threshold = max(0.0, min_interval - max(0.0, float(jitter_tolerance_ms)))

    last = last_stm_ms
    kept: list[dict[str, Any]] = []
    for row in rows:
        stamp = get_stamp(row)
        if stamp is None:
            kept.append(row)
            continue
        if last is not None and stamp + RESET_BACKWARD_MS < float(last):
            last = None
        if last is not None and (stamp - float(last)) < threshold:
            continue
        kept.append(row)
        last = stamp
    return kept, last
