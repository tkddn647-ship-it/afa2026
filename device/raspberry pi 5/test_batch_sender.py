"""BatchSender 재전송 테스트 (차량·UART·서버 없이 실행).

가짜 HTTP 세션으로 서버 장애를 흉내 낸다. 수정 전에는 POST 가 실패한 배치가
그대로 사라졌다. 수정 후에는 다음 주기에 순서대로 다시 보내야 한다.

실행: python -m unittest test_batch_sender   ("device/raspberry pi 5" 에서)
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# 시험 환경에는 pyserial 이 없을 수 있다. Uart_stm 이 import 만 통과하도록 빈 모듈을 넣는다.
if "serial" not in sys.modules:
    try:
        import serial  # noqa: F401
    except ImportError:
        fake = types.ModuleType("serial")
        fake.Serial = object
        fake.SerialException = Exception
        tools = types.ModuleType("serial.tools")
        list_ports = types.ModuleType("serial.tools.list_ports")
        list_ports.comports = lambda: []
        tools.list_ports = list_ports
        fake.tools = tools
        sys.modules.update({"serial": fake, "serial.tools": tools, "serial.tools.list_ports": list_ports})

import requests  # noqa: E402

import data_send_server as dss  # noqa: E402


class FakeResponse:
    status_code = 200

    def raise_for_status(self):
        return None


class FakeSession:
    """fail_calls 에 든 순번의 POST 는 타임아웃으로 실패시킨다."""

    def __init__(self, fail_calls=()):
        self.fail_calls = set(fail_calls)
        self.calls = 0
        self.received: list[int] = []
        self.post_sizes: list[int] = []

    def mount(self, *args, **kwargs):
        return None

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls += 1
        if self.calls in self.fail_calls:
            raise requests.Timeout("simulated timeout")
        samples = json.loads(data)["samples"]
        self.received.extend(s["t"] for s in samples)
        self.post_sizes.append(len(samples))
        return FakeResponse()


def make_sample(t: int) -> dict:
    return {"stm_ms": t, "FR": 0.0, "FL": 0.0, "RR": 0.0, "RL": 0.0, "x_g": 0.0, "y_g": 0.0, "z_g": 1.0}


def make_sender(fail_calls=()):
    sender = dss.BatchSender("http://test.invalid/ingest", "test-device", timeout=0.1)
    sender.session = FakeSession(fail_calls)
    return sender


def batches(n_batches: int, size: int = 5):
    t = 1000
    for _ in range(n_batches):
        yield [make_sample(t + 10 * k) for k in range(size)]
        t += 10 * size


class RetryKeepsEverySample(unittest.TestCase):
    def test_no_failure_sends_everything_in_order(self):
        sender = make_sender()
        expected = []
        for batch in batches(40):
            expected += [s["stm_ms"] for s in batch]
            self.assertTrue(sender.send(batch))
        self.assertEqual(sender.session.received, expected)
        self.assertEqual(sender.resent_samples, 0)

    def test_single_failure_is_resent_next_cycle(self):
        sender = make_sender(fail_calls={3})
        expected = []
        results = []
        for batch in batches(10):
            expected += [s["stm_ms"] for s in batch]
            results.append(sender.send(batch))
        self.assertEqual(results.count(False), 1)
        self.assertEqual(sender.session.received, expected)
        self.assertEqual(sender.resent_samples, 5)
        self.assertEqual(sender.dropped_samples, 0)
        self.assertEqual(sender.pending, [])

    def test_two_second_outage_loses_nothing(self):
        # 50 ms 주기 40번 = 2초 동안 서버가 응답하지 않는 경우
        sender = make_sender(fail_calls=set(range(11, 51)))
        expected = []
        for batch in batches(120):
            expected += [s["stm_ms"] for s in batch]
            sender.send(batch)
        self.assertEqual(sender.session.received, expected)
        self.assertEqual(sender.dropped_samples, 0)
        self.assertEqual(sender.pending, [])
        self.assertLessEqual(max(sender.session.post_sizes), dss.POST_MAX_SAMPLES)

    def test_order_is_preserved_across_outage(self):
        sender = make_sender(fail_calls={2, 3, 4, 9})
        for batch in batches(30):
            sender.send(batch)
        got = sender.session.received
        self.assertEqual(got, sorted(got))
        self.assertEqual(len(got), len(set(got)))

    def test_long_outage_drops_oldest_and_counts_them(self):
        sender = make_sender(fail_calls=set(range(1, 10_000)))
        sender.pending_max_samples = 100
        total = 0
        for batch in batches(60):
            total += len(batch)
            self.assertFalse(sender.send(batch))
        self.assertEqual(len(sender.pending), 100)
        self.assertEqual(sender.dropped_samples, total - 100)
        # 남아 있는 것은 가장 최근 100개
        self.assertEqual(sender.pending[-1]["stm_ms"], 1000 + 10 * (total - 1))

    def test_realtime_gets_only_fresh_batch(self):
        sender = make_sender(fail_calls={2})
        sender.realtime_url = "http://test.invalid/live"
        posts = []
        original = sender._post_json

        def spy(url, body):
            original(url, body)
            posts.append((url, len(body["samples"])))

        sender._post_json = spy
        for batch in batches(4):
            sender.send(batch)
        live = [n for url, n in posts if url.endswith("/live")]
        self.assertTrue(all(n == 5 for n in live))


if __name__ == "__main__":
    unittest.main()
