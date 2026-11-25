"""log_downsample 회귀 테스트.

펌웨어(sensor_uart.c) 타이밍을 재현한다: 10 ms 주기 샘플, 200 ms 마다 MCU 온도를
읽는 루프에서 찍힌 샘플은 타임스탬프가 1 ms 늦다. 실제 로그(2026-07~08)의
"간격 11 ms 뒤 19 ms, 200 ms 주기, 약 5% 손실" 패턴이 필터에서 생기는지 확인하고,
수정 후에는 생기지 않는지 확인한다.

실행: python -m unittest test_log_downsample   (server/udp_logger 에서)
"""

from __future__ import annotations

import sys
import unittest
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from log_downsample import downsample_rows  # noqa: E402


def firmware_stamps(n_samples: int, *, period_ms: int = 10, temp_every: int = 20, start_ms: int = 47_008) -> list[int]:
    """STM 타임스탬프 열. temp_every 번째 샘플마다 1 ms 늦는다."""
    stamps = []
    for k in range(n_samples):
        late = 1 if (k % temp_every) == temp_every - 1 else 0
        stamps.append(start_ms + k * period_ms + late)
    return stamps


def run_filter(stamps, *, tolerance, batch=10, min_interval=10.0):
    """배치 단위로 필터를 통과시킨다 (서버가 POST 마다 호출하는 것과 같게)."""
    last = None
    out: list[int] = []
    for i in range(0, len(stamps), batch):
        rows = [{"stm_ms": s} for s in stamps[i : i + batch]]
        kept, last = downsample_rows(
            rows,
            last,
            min_interval_ms=min_interval,
            jitter_tolerance_ms=tolerance,
            get_stamp=lambda row: None if row["stm_ms"] is None else float(row["stm_ms"]),
        )
        out.extend(r["stm_ms"] for r in kept)
    return out


class OldBehaviourReproducesLogSignature(unittest.TestCase):
    """허용치 0 = 수정 전 동작. 주행 로그에서 본 패턴이 그대로 나와야 한다."""

    def setUp(self):
        self.stamps = firmware_stamps(20_000)
        self.kept = run_filter(self.stamps, tolerance=0.0)

    def test_five_percent_lost(self):
        lost = 1 - len(self.kept) / len(self.stamps)
        self.assertAlmostEqual(lost, 0.05, places=3)

    def test_gap_pattern_is_11_then_19(self):
        gaps = [b - a for a, b in zip(self.kept, self.kept[1:])]
        counts = Counter(gaps)
        self.assertEqual(set(counts), {10, 11, 19})
        # 마지막 샘플이 늦은 샘플이면 그 뒤의 19 ms 간격은 열 끝에서 잘린다.
        self.assertLessEqual(abs(counts[11] - counts[19]), 1)
        for i, g in enumerate(gaps):
            if g == 19:
                self.assertEqual(gaps[i - 1], 11)

    def test_loss_repeats_every_200_ms(self):
        gaps = [b - a for a, b in zip(self.kept, self.kept[1:])]
        at = [self.kept[i] for i, g in enumerate(gaps) if g == 19]
        self.assertEqual({b - a for a, b in zip(at, at[1:])}, {200})


class FixedBehaviour(unittest.TestCase):
    def test_no_sample_lost_with_default_tolerance(self):
        stamps = firmware_stamps(20_000)
        self.assertEqual(run_filter(stamps, tolerance=None), stamps)

    def test_no_sample_lost_for_any_batch_size(self):
        stamps = firmware_stamps(5_000)
        for batch in (1, 5, 9, 10, 19, 20, 37):
            self.assertEqual(run_filter(stamps, tolerance=None, batch=batch), stamps, batch)

    def test_200hz_source_still_decimated_to_100hz(self):
        stamps = [1000 + 5 * k for k in range(4000)]
        kept = run_filter(stamps, tolerance=None)
        self.assertEqual(len(kept), 2000)
        self.assertEqual({b - a for a, b in zip(kept, kept[1:])}, {10})

    def test_exact_duplicates_dropped(self):
        stamps = [100, 110, 110, 120, 120, 120, 130]
        self.assertEqual(run_filter(stamps, tolerance=None), [100, 110, 120, 130])

    def test_resent_batch_is_dropped(self):
        """Pi 가 타임아웃 후 같은 배치를 다시 보내도 중복 행이 생기지 않는다."""
        first = firmware_stamps(40)
        self.assertEqual(run_filter(first + first[30:], tolerance=None), first)

    def test_reset_after_stm_reboot(self):
        stamps = [500_000, 500_010, 500_020, 300, 310, 320]
        self.assertEqual(run_filter(stamps, tolerance=None), stamps)

    def test_rows_without_clock_are_kept(self):
        rows = [{"stm_ms": 100}, {"stm_ms": None}, {"stm_ms": 104}, {"stm_ms": 110}]
        kept, last = downsample_rows(
            rows, None, min_interval_ms=10.0, get_stamp=lambda r: None if r["stm_ms"] is None else float(r["stm_ms"])
        )
        self.assertEqual([r["stm_ms"] for r in kept], [100, None, 110])
        self.assertEqual(last, 110.0)


if __name__ == "__main__":
    unittest.main()
