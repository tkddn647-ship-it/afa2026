"""UART 줄 무결성 테스트 (차량·STM 없이 실행).

주행 로그에서 STM→Pi UART 줄에 단일 비트 오류('0'→'1','2','4','8')가 확인됐다.
구 파서(parse_line)는 체크섬이 없고 필드가 모자라면 0으로 채워서, 손상된 줄을 그대로 받았다.
LineGuard 는 펌웨어가 붙이는 ",*XXXX"(CRC-16/CCITT-FALSE)와 필드 수 고정으로 이런 줄을 버린다.

실행: python -m unittest test_uart_line_guard   ("device/raspberry pi 5" 에서)
"""

from __future__ import annotations

import random
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

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

import Uart_stm as U  # noqa: E402

FIRMWARE_CRC_H = HERE.parent / "ECU_test" / "Core" / "Inc" / "line_crc.h"


def make_body(rng: random.Random, stm_ms: int) -> str:
    """펌웨어 SensorUart_FormatSample 과 같은 자릿수의 35필드 한 줄 (꼬리 없이)."""
    f = [str(stm_ms)]
    f += [f"{rng.uniform(-30, 30):.2f}" for _ in range(4)]          # FR FL RR RL (mm)
    f += [f"{rng.uniform(-1.5, 1.5):.3f}" for _ in range(3)]        # x y z (g)
    f += [f"{rng.uniform(40, 50):.1f}", "0.0", "0"]                 # ecu_temp, steering
    f += [f"{rng.uniform(30, 45):.1f}", f"{rng.uniform(30, 50):.1f}", str(rng.randint(0, 3500)),
          f"{rng.uniform(0, 200):.1f}", f"{rng.uniform(230, 250):.1f}", "0", "1", "0"]
    f += ["0.0"] + [f"{rng.uniform(-50, 300):.1f}" for _ in range(6)]  # shudder (always 0) + id/iq/torque
    f += [f"{rng.uniform(40, 90):.1f}", f"{rng.uniform(230, 250):.1f}", f"{rng.uniform(0, 150):.1f}", "200", "300",
          f"{rng.uniform(25, 45):.1f}", f"{rng.uniform(10, 20):.1f}", "1", "1"]
    return ",".join(f)


def with_crc(body: str) -> str:
    return f"{body},*{U.line_crc16(body.encode()):04X}"


def flip_stream(lines: list[str], p: float, rng: random.Random) -> list[str]:
    """줄들을 '\\n' 으로 이어 보낸 바이트열에 독립 비트 오류를 넣고, Pi 처럼 '\\n' 으로 다시 자른다."""
    raw = bytearray(("\n".join(lines) + "\n").encode())
    for i in range(len(raw)):
        for b in range(8):
            if rng.random() < p:
                raw[i] ^= 1 << b
    return [chunk.decode("ascii", errors="ignore") for chunk in bytes(raw).split(b"\n")]


class LineCrcTest(unittest.TestCase):
    def test_check_value(self) -> None:
        self.assertEqual(U.line_crc16(b"123456789"), 0x29B1)

    @unittest.skipUnless(shutil.which("gcc") and FIRMWARE_CRC_H.exists(), "gcc 또는 펌웨어 헤더 없음")
    def test_firmware_header_matches_python(self) -> None:
        rng = random.Random(3)
        bodies = [make_body(rng, 1000 + 10 * i) for i in range(50)] + ["", "123456789"]
        src = (
            f'#include <stdio.h>\n#include <string.h>\n#include "{FIRMWARE_CRC_H}"\n'
            "int main(void){char b[1024];while(fgets(b,sizeof b,stdin)){int n=(int)strcspn(b,\"\\n\");"
            "printf(\"%04X\\n\",(unsigned)LineCrc16(b,n));}return 0;}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            c, exe = Path(tmp) / "t.c", Path(tmp) / "t"
            c.write_text(src)
            subprocess.run(["gcc", "-O2", "-o", str(exe), str(c)], check=True)
            out = subprocess.run([str(exe)], input="\n".join(bodies) + "\n", capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(out, [f"{U.line_crc16(b.encode()):04X}" for b in bodies])


class LineGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rng = random.Random(7)

    def test_valid_crc_line_accepted_and_single_flip_rejected(self) -> None:
        g = U.LineGuard()
        body = make_body(self.rng, 5000)
        self.assertEqual(g.check(with_crc(body)), (body, "ok"))
        i = body.index("0.0", body.index(",", 60))          # a zero digit in the middle of the line
        bad = body[:i] + "8" + body[i + 1:]                   # '0' -> '8' (bit 3), as seen in the logs
        self.assertEqual(g.check(f"{bad},*{U.line_crc16(body.encode()):04X}")[1], "crc")

    def test_line_without_crc_rejected_once_crc_seen(self) -> None:
        g = U.LineGuard()
        g.check(with_crc(make_body(self.rng, 10)))
        self.assertEqual(g.check(make_body(self.rng, 20))[1], "no_crc")

    def test_old_firmware_without_crc_still_accepted(self) -> None:
        g = U.LineGuard()
        for i in range(100):
            body, reason = g.check(make_body(self.rng, 10 * i))
            self.assertEqual(reason, "ok")
        self.assertEqual(g.locked_count, 35)

    def test_truncated_line_rejected_while_old_parser_zero_fills_it(self) -> None:
        g = U.LineGuard()
        body = make_body(self.rng, 1234)
        short = ",".join(body.split(",")[:20])                # bytes lost mid-line
        self.assertIsNotNone(U.parse_line(short))              # old parser: accepted, missing fields = 0
        self.assertEqual(g.check(short)[1], "fields")

    def test_merged_lines_rejected(self) -> None:
        g = U.LineGuard()
        merged = make_body(self.rng, 100) + make_body(self.rng, 110)   # lost '\n'
        self.assertIsNotNone(U.parse_line(merged))             # old parser keeps the first 34 values
        self.assertEqual(g.check(merged)[1], "fields")

    def test_old_parser_ignores_crc_tail(self) -> None:
        """새 펌웨어 + 구 Pi 파서 조합도 동작해야 한다 (배포 순서와 무관)."""
        body = make_body(self.rng, 777)
        self.assertEqual(U.parse_line(with_crc(body)), U.parse_line(body))

    def test_no_wrong_values_accepted_under_bit_errors(self) -> None:
        """로그에서 추정한 비트 오류율 최대치(약 3e-4/bit) 근처로 오류를 넣으면 구 파서는 틀린 값을 받고, 새 경로는 받지 않는다."""
        truth = {}
        lines = []
        for i in range(1500):
            body = make_body(self.rng, 10_000 + 10 * i)
            truth[10_000 + 10 * i] = U.parse_line(body)
            lines.append(with_crc(body))
        received = flip_stream(lines, 3e-4, random.Random(11))
        old_wrong = new_wrong = new_ok = 0
        g = U.LineGuard()
        for line in received:
            s = U.parse_line(line)
            if s is not None and s != truth.get(s.get("stm_ms")):
                old_wrong += 1
            body, reason = g.check(line)
            if body is None:
                continue
            s = U.parse_line(body)
            if s is not None and s == truth.get(s.get("stm_ms")):
                new_ok += 1
            elif s is not None:
                new_wrong += 1
        self.assertGreater(old_wrong, 50)
        self.assertEqual(new_wrong, 0)
        self.assertGreater(new_ok, 800)


if __name__ == "__main__":
    unittest.main()
