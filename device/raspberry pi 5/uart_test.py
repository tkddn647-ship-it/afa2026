#!/usr/bin/env python3
"""Pi GPIO UART 점검: 루프백, STM 수신 대기."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

try:
    import serial
except ImportError:
    print("pip install pyserial")
    sys.exit(1)

from Uart_stm import (
    DEFAULT_BAUD,
    GPIO_UART_DEVICE,
    is_pi5_header_uart,
    open_uart_serial,
    print_pi5_gpio_uart_warning,
    resolve_uart_port,
)


def print_pi_uart_info(port: str) -> None:
    resolved = resolve_uart_port(port)
    print(f"[info] 요청 포트: {port}")
    print(f"[info] 사용 포트: {resolved or '(없음)'}")
    if os.path.exists("/dev/serial0"):
        print(f"[info] /dev/serial0 -> {os.path.realpath('/dev/serial0')}")
    if os.path.exists(GPIO_UART_DEVICE):
        print(f"[info] {GPIO_UART_DEVICE} = GPIO Pin 8/10 (STM 배선용)")


def _uart_port_holders(port: str) -> list[str]:
    holders: list[str] = []
    try:
        result = subprocess.run(
            ["fuser", "-v", port],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in result.stderr.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-1].isdigit():
                holders.append(f"PID {parts[-1]} ({parts[-2]})")
    except OSError:
        pass
    return holders


def ensure_uart_port_free(port: str, stop_service: bool) -> bool:
    """루프백/점검 전 UART 포트 점유 해제. False 면 호출자가 중단해야 함."""
    service = "stm-data-sender.service"
    try:
        active = (
            subprocess.run(
                ["systemctl", "--user", "is-active", service],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            == "active"
        )
    except OSError:
        active = False

    holders = _uart_port_holders(port)
    if not active and not holders:
        return True

    if active:
        print(f"[warn] {service} 가 {port} 를 사용 중입니다.")
        if stop_service:
            subprocess.run(["systemctl", "--user", "stop", service], check=False)
            time.sleep(0.3)
            print(f"[info] {service} 중지함")
            return True
        print("       루프백/점검 전 아래 실행:")
        print("         systemctl --user stop stm-data-sender.service")
        return False

    if holders:
        print(f"[warn] {port} 점유 중: {', '.join(holders)}")
        print("       위 프로세스 종료 후 다시 시도")
        return False
    return True


def _drain_rx(ser: serial.Serial, seconds: float = 0.15) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not ser.read(256):
            break
    ser.reset_input_buffer()


def loopback_test(port: str, baud: int, stop_service: bool) -> bool:
    print("\n[loopback] Pi Pin 8(TX) 와 Pin 10(RX) 점퍼 연결 후 테스트 (STM 분리)")
    if not ensure_uart_port_free(port, stop_service):
        return False
    payload = b"LOOPBACK_TEST\n"
    path = port
    last_got = b""
    for attempt in range(5):
        ser = open_uart_serial(port, baud, timeout=0.2)
        path = ser.port or port
        _drain_rx(ser)
        ser.write(payload)
        ser.flush()
        time.sleep(0.12)
        got = ser.read(128)
        ser.close()
        last_got = got
        if payload in got or b"LOOPBACK_TEST" in got:
            print(f"[ok] 루프백 성공 @ {path} {baud}")
            return True
        time.sleep(0.05)
    print(f"[fail] 루프백 실패 (수신={last_got!r})")
    if is_pi5_header_uart(path):
        print_pi5_gpio_uart_warning(path)
    else:
        print("  -> STM 전원/배선 분리 후 Pin8-Pin10 점퍼 확인")
    return False


def listen(port: str, baud: int, seconds: float) -> int:
    ser = open_uart_serial(port, baud, timeout=0.2)
    path = ser.port or port
    print(f"\n[listen] {path} @ {baud} for {seconds:.0f}s")
    print("  기대: STM_EARLY / STM_READY / HB,1234 / 센서 CSV")
    _drain_rx(ser, 0.1)
    end = time.time() + seconds
    lines = 0
    while time.time() < end:
        raw = ser.readline()
        if raw:
            lines += 1
            print(f"  << {raw.decode('ascii', errors='replace').rstrip()}")
    ser.close()
    print(f"  수신 줄 수: {lines}")
    return lines


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="UART 연결 점검")
    p.add_argument("--port", default="/dev/ttyAMA0")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    p.add_argument("--seconds", type=float, default=5.0)
    p.add_argument("--try-bauds", default="460800,115200,230400")
    p.add_argument("--loopback", action="store_true")
    p.add_argument(
        "--stop-service",
        action="store_true",
        help="루프백 전 stm-data-sender 서비스 자동 중지",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    print_pi_uart_info(args.port)
    print("[info] STM 배선: Pi8(TX)->STM RX(PC11), Pi10(RX)->STM TX(PC10), GND")

    if args.loopback:
        sys.exit(0 if loopback_test(args.port, args.baud, args.stop_service) else 1)

    if not ensure_uart_port_free(args.port, args.stop_service):
        sys.exit(1)

    total = 0
    for baud in [int(x.strip()) for x in args.try_bauds.split(",") if x.strip()]:
        total += listen(args.port, baud, args.seconds)
        if total > 0:
            print(f"\n[ok] 보드레이트 {baud} 에서 STM 수신 확인")
            return

    print("\n[fail] STM 수신 없음")
    print("  1) STM 리셋 버튼 누르거나 전원 재인가")
    print("  2) TX/RX 교차·GND 확인 (Pi8->STM_RX, Pi10->STM_TX)")
    print("  3) STM 펌웨어 UART4 460800 / PC10(TX) PC11(RX) 확인")
    print("  4) USB-TTL로 STM TX만 PC에 연결해 시리얼 모니터 확인")
    sys.exit(1)


if __name__ == "__main__":
    main()
