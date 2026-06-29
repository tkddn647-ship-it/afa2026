"""
STM32(UART4, 460800) -> Raspberry Pi 5 GPIO UART 수신

하드웨어 (40핀 GPIO):
  Pi Pin 8  (GPIO14 TXD) -> STM32 UART4 RX (PC11)
  Pi Pin 10 (GPIO15 RXD) -> STM32 UART4 TX (PC10)
  Pi GND (Pin 6 등)     -> STM32 GND
  소프트웨어 포트: /dev/ttyAMA0 (Pi5 GPIO Pin 8/10, dtparam=uart0=on)

  주의: Pi5 에서 /dev/serial0 은 ttyAMA10(보드 옆 3핀 UART)일 수 있음.
        GPIO 8/10 배선이면 반드시 /dev/ttyAMA0 사용.

- 200 Hz: FR, FL, RR, RL, x_g, y_g, z_g + timestamp 를 data[] 에 적재
- 20 Hz(50 ms): data[] 를 배치로 넘긴 뒤 초기화

STM32 한 줄 형식 (CSV, \\n 종료):
  FR,FL,RR,RL,x_g,y_g,z_g
또는 STM32 타임스탬프(ms) + MCU 칩 내부 온도 + LWS 조향 + 휠스피드 포함:
  stm_ms,FR,FL,RR,RL,x_g,y_g,z_g,ecu_temp,steering_angle,steering_speed,wheel_rpm_right,wheel_rpm_left
  ecu_temp = STM32 MCU 칩 내부(다이) 온도(°C)
  steering_angle = Bosch LWS 조향각(°), steering_speed = 조향 속도(°/s)
  wheel_rpm_right / wheel_rpm_left = STM32 계산 좌·우 바퀴 RPM
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("[error] pyserial 이 필요합니다.  pip install pyserial")
    sys.exit(1)


DEFAULT_BAUD = 460800
DEFAULT_UART_PORT = "auto"
UART_MODULE_VERSION = "2026-03-24-gpio4"

# Pi5 GPIO Pin 8/10 (GPIO14/15). dtparam=uart0=on 필요.
GPIO_UART_DEVICE = "/dev/ttyAMA0"

# 구버전 스크립트 기본값. 장치가 없으면 auto 로 처리.
LEGACY_UART_PORTS = frozenset({"/dev/ttyAMA0", "ttyAMA0"})

# auto 탐색 순서. ttyS0 는 GPIO 8/10 이 아님 — 제외.
PI_UART_CANDIDATES = (
    "/dev/ttyAMA0",
    "/dev/serial0",
    "/dev/ttyAMA10",
    "/dev/ttyUSB0",
    "/dev/ttyACM0",
)

SAMPLE_RATE_HZ = 200
FLUSH_RATE_HZ = 20
FLUSH_INTERVAL_S = 1.0 / FLUSH_RATE_HZ
SAMPLES_PER_BATCH = SAMPLE_RATE_HZ // FLUSH_RATE_HZ  # 10

LINEAR_KEYS = ("FR", "FL", "RR", "RL")
ACCEL_KEYS = ("x_g", "y_g", "z_g")
ECU_TEMP_KEY = "ecu_temp"
STEERING_KEYS = ("steering_angle", "steering_speed")
WHEEL_KEYS = ("wheel_rpm_right", "wheel_rpm_left")
SENSOR_KEYS = LINEAR_KEYS + ACCEL_KEYS + (ECU_TEMP_KEY,) + STEERING_KEYS + WHEEL_KEYS
LEGACY_SENSOR_KEYS = LINEAR_KEYS + ACCEL_KEYS
SENSOR_KEYS_WITH_TEMP = LINEAR_KEYS + ACCEL_KEYS + (ECU_TEMP_KEY,)
SENSOR_KEYS_WITH_STEERING = LINEAR_KEYS + ACCEL_KEYS + (ECU_TEMP_KEY,) + STEERING_KEYS
BOOT_LINE_PREFIXES = ("STM_", "HB,", "LWS_CAL")

# 수신 버퍼 (20 Hz 마다 초기화)
data: list[dict[str, Any]] = []
_data_lock = threading.Lock()
_uart_debug = False


class UartRxStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.raw_bytes = 0
        self.raw_lines = 0
        self.parsed_samples = 0
        self.parse_errors = 0
        self.last_raw_line = ""
        self.last_bad_line = ""

    def on_bytes(self, nbytes: int) -> None:
        with self._lock:
            self.raw_bytes += nbytes

    def on_line(self, line: str, sample: dict[str, Any] | None) -> None:
        with self._lock:
            self.raw_lines += 1
            self.last_raw_line = line[:120]
            if sample is None:
                if line.strip():
                    self.parse_errors += 1
                    self.last_bad_line = line[:120]
            else:
                self.parsed_samples += 1

    def on_boot_line(self, line: str) -> None:
        with self._lock:
            self.raw_lines += 1
            self.last_raw_line = line[:120]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "raw_bytes": self.raw_bytes,
                "raw_lines": self.raw_lines,
                "parsed_samples": self.parsed_samples,
                "parse_errors": self.parse_errors,
                "last_raw_line": self.last_raw_line,
                "last_bad_line": self.last_bad_line,
            }


uart_stats = UartRxStats()


def set_uart_debug(enabled: bool) -> None:
    global _uart_debug
    _uart_debug = enabled


def _path_exists(path: str) -> bool:
    try:
        return Path(path).exists()
    except OSError:
        return False


def list_available_uart_ports() -> list[str]:
    found: list[str] = []
    for path in PI_UART_CANDIDATES:
        if _path_exists(path) and path not in found:
            found.append(path)
    try:
        for info in list_ports.comports():
            if info.device not in found:
                found.append(info.device)
    except Exception:
        pass
    return found


def _device_realpath(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return ""


def is_pi5_header_uart(path: str) -> bool:
    return _device_realpath(path).endswith("ttyAMA10")


def print_pi5_gpio_uart_warning(path: str) -> None:
    if not is_pi5_header_uart(path):
        return
    print("[error] /dev/serial0 -> ttyAMA10 = Pi5 보드 옆 3핀 UART 입니다.")
    print("        GPIO Pin 8/10 과는 다른 포트라 루프백/STM 수신이 안 됩니다.")
    print("[fix] /boot/firmware/config.txt 맨 아래 추가 후 reboot:")
    print("        enable_uart=1")
    print("        dtparam=uart0=on")
    print("        dtoverlay=disable-bt")
    print("[fix] 재부팅 후 확인:")
    print("        ls -l /dev/ttyAMA0")
    print("        python uart_test.py --port /dev/ttyAMA0 --loopback")


def resolve_uart_port(port: str = DEFAULT_UART_PORT) -> str:
    normalized = port.strip()
    lowered = normalized.lower()

    if lowered == "auto":
        if _path_exists(GPIO_UART_DEVICE):
            return GPIO_UART_DEVICE
        if _path_exists("/dev/serial0") and not is_pi5_header_uart("/dev/serial0"):
            return "/dev/serial0"
        if _path_exists("/dev/serial0") and is_pi5_header_uart("/dev/serial0"):
            print_pi5_gpio_uart_warning("/dev/serial0")
            return ""
        for candidate in PI_UART_CANDIDATES:
            if _path_exists(candidate):
                return candidate
        return ""

    if normalized in ("/dev/serial0", "serial0") and is_pi5_header_uart("/dev/serial0"):
        if _path_exists(GPIO_UART_DEVICE):
            print("[warn] /dev/serial0 은 ttyAMA10 입니다. GPIO용 /dev/ttyAMA0 사용")
            return GPIO_UART_DEVICE
        print_pi5_gpio_uart_warning("/dev/serial0")
        return ""

    if normalized in LEGACY_UART_PORTS and not _path_exists(normalized):
        if _path_exists(GPIO_UART_DEVICE):
            return GPIO_UART_DEVICE
        if _path_exists("/dev/serial0") and not is_pi5_header_uart("/dev/serial0"):
            return "/dev/serial0"
        print_pi5_gpio_uart_warning("/dev/serial0")
        return ""

    if not _path_exists(normalized):
        if _path_exists(GPIO_UART_DEVICE):
            return GPIO_UART_DEVICE
        return ""

    if is_pi5_header_uart(normalized):
        print_pi5_gpio_uart_warning(normalized)
        return ""

    return normalized


def uart_open_candidates(port: str) -> list[str]:
    resolved = resolve_uart_port(port)
    requested = port.strip().lower()

    if not resolved:
        return []

    # 명시적 포트 지정 시 다른 UART 로 넘어가지 않음 (ttyS0 오동작 방지)
    if requested not in ("auto", ""):
        candidates = [resolved]
        if resolved != "/dev/serial0" and _path_exists("/dev/serial0"):
            if _device_realpath("/dev/serial0") == _device_realpath(resolved):
                candidates.append("/dev/serial0")
        return candidates

    candidates: list[str] = [resolved]
    for path in PI_UART_CANDIDATES:
        if _path_exists(path) and path not in candidates:
            candidates.append(path)
    return candidates


def _print_open_failure_hints(path: str, exc: Exception) -> None:
    err = str(exc).lower()
    print(f"[error] {path} 열기 실패: {exc}")
    if "permission" in err or "errno 13" in err or "acces" in err:
        print("[hint] sudo usermod -aG dialout $USER 후 로그아웃/재로그인")
    if "lock" in err or "errno 11" in err or "busy" in err:
        print("[hint] sudo fuser -v /dev/ttyAMA0 /dev/serial0")
        print("[hint] sudo systemctl stop serial-getty@ttyAMA0.service")
        print("[hint] miniterm 등 다른 프로그램 종료 (Ctrl+])")


def print_uart_port_help(requested: str = "") -> None:
    if requested:
        print(f"[hint] 요청한 포트: {requested}")
    ports = list_available_uart_ports()
    print("[hint] 사용 가능한 UART 포트:")
    if ports:
        for path in ports:
            resolved = ""
            try:
                if _path_exists(path):
                    resolved = f" -> {os.path.realpath(path)}"
            except OSError:
                pass
            print(f"  {path}{resolved}")
    else:
        print("  (없음)")
    print("[hint] 포트 점유 시:")
    print("  sudo fuser -v /dev/serial0")
    print("  miniterm 종료: Ctrl+]")
    print("[hint] 시리얼 콘솔 끄기 (데이터 가로채기 방지):")
    print("  sudo systemctl stop serial-getty@ttyAMA0.service")
    print("  sudo systemctl disable serial-getty@ttyAMA0.service")
    print("[hint] Pi 5 GPIO Pin 8/10 (STM 연결):")
    print("  소프트웨어: /dev/ttyAMA0  (serial0 이 ttyAMA10 이면 잘못된 포트!)")
    print("  /boot/firmware/config.txt:")
    print("    enable_uart=1")
    print("    dtparam=uart0=on")
    print("    dtoverlay=disable-bt")
    print("  sudo reboot && ls -l /dev/ttyAMA0")
    print("[hint] 배선:")


def open_uart_serial(port: str, baud: int, timeout: float) -> serial.Serial:
    requested = port.strip()
    candidates = uart_open_candidates(port)
    if not candidates:
        print(f"[error] UART 포트를 찾을 수 없습니다. (uart_stm {UART_MODULE_VERSION})")
        print_uart_port_help(requested)
        sys.exit(1)

    last_exc: Exception | None = None
    explicit = requested.lower() not in ("auto", "")
    for path in candidates:
        try:
            ser = serial.Serial(path, baud, timeout=timeout)
        except serial.SerialException as exc:
            last_exc = exc
            err = str(exc).lower()
            if "lock" in err or "errno 11" in err or "resource temporarily unavailable" in err:
                print(f"[error] {path} 사용 중 (다른 프로그램이 점유): {exc}")
                print("[hint] miniterm 종료: Ctrl+]  (Ctrl+C 아님)")
                print("       sudo fuser -v /dev/ttyAMA0 /dev/serial0")
                print("       sudo kill <PID>")
                sys.exit(1)
            if explicit:
                _print_open_failure_hints(path, exc)
                sys.exit(1)
            continue

        if requested.lower() == "auto":
            print(f"[info] UART 포트 자동 선택: {path}")
        elif path != requested:
            print(f"[info] {requested} 와 동일 장치 {path} 사용")
        try:
            print(f"[info] realpath: {os.path.realpath(path)}")
        except OSError:
            pass
        ser.reset_input_buffer()
        return ser

    print(f"[error] UART 열기 실패: {last_exc}")
    print(f"[info] uart_stm 버전: {UART_MODULE_VERSION}")
    print_uart_port_help(requested)
    sys.exit(1)


def parse_line(line: str) -> dict[str, Any] | None:
    """UART 한 줄을 센서 샘플 dict 로 변환."""
    parts = [p.strip() for p in line.strip().split(",")]
    if len(parts) < len(LEGACY_SENSOR_KEYS):
        return None

    stm_ms: int | None = None
    values: list[str]
    if len(parts) >= len(LEGACY_SENSOR_KEYS) + 1:
        try:
            stm_ms = int(float(parts[0]))
            values = parts[1:]
        except ValueError:
            values = parts
    else:
        values = parts

    if len(values) == len(LEGACY_SENSOR_KEYS):
        values = [*values, "0", "0", "0", "0", "0"]
    elif len(values) == len(SENSOR_KEYS_WITH_TEMP):
        values = [*values, "0", "0", "0", "0"]
    elif len(values) == len(SENSOR_KEYS_WITH_STEERING):
        values = [*values, "0", "0"]
    elif len(values) != len(SENSOR_KEYS):
        return None

    try:
        nums = [float(v) for v in values]
    except ValueError:
        return None

    sample: dict[str, Any] = {
        "timestamp": (stm_ms / 1000.0) if stm_ms is not None else time.time(),
    }
    if stm_ms is not None:
        sample["stm_ms"] = stm_ms
    for key, val in zip(SENSOR_KEYS, nums):
        sample[key] = val
    return sample


def append_sample(sample: dict[str, Any]) -> None:
    with _data_lock:
        data.append(sample)


def flush_batch(on_batch: Callable[[list[dict[str, Any]]], None] | None = None) -> list[dict[str, Any]]:
    """data[] 를 복사해 반환하고 초기화."""
    with _data_lock:
        batch = list(data)
        data.clear()
    if batch and on_batch is not None:
        on_batch(batch)
    return batch


def flush_loop(
    stop_event: threading.Event,
    on_batch: Callable[[list[dict[str, Any]]], None] | None,
) -> None:
    next_flush = time.monotonic() + FLUSH_INTERVAL_S
    while not stop_event.is_set():
        now = time.monotonic()
        if now >= next_flush:
            batch = flush_batch(on_batch)
            if batch and on_batch is None:
                print(
                    f"[batch] n={len(batch)} "
                    f"FR={batch[-1]['FR']:.2f} FL={batch[-1]['FL']:.2f} "
                    f"x_g={batch[-1]['x_g']:.3f}"
                )
            next_flush += FLUSH_INTERVAL_S
            if now - next_flush > FLUSH_INTERVAL_S:
                next_flush = now + FLUSH_INTERVAL_S
        stop_event.wait(0.001)


def is_boot_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    return stripped.startswith(BOOT_LINE_PREFIXES)


def read_loop(
    ser: serial.Serial,
    stop_event: threading.Event,
) -> None:
    while not stop_event.is_set():
        try:
            raw = ser.readline()
        except serial.SerialException as exc:
            print(f"[error] serial read: {exc}")
            break
        if not raw:
            continue
        uart_stats.on_bytes(len(raw))
        try:
            line = raw.decode("ascii", errors="ignore")
        except UnicodeDecodeError:
            continue
        if is_boot_line(line):
            uart_stats.on_boot_line(line)
            if _uart_debug and line.strip():
                print(f"[uart:boot] {line.strip()[:100]}")
            continue
        sample = parse_line(line)
        uart_stats.on_line(line, sample)
        if _uart_debug:
            tag = "ok" if sample is not None else "bad"
            print(f"[uart:{tag}] {line.strip()[:100]}")
        if sample is None:
            continue
        append_sample(sample)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="STM32 UART 센서 수신 (200 Hz / 20 Hz flush)")
    parser.add_argument(
        "--port",
        default=DEFAULT_UART_PORT,
        help="UART 디바이스. auto=자동탐지 (Pi5: /dev/serial0 또는 /dev/ttyAMA10)",
    )
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="보드레이트. STM32 UART4 와 동일")
    parser.add_argument("--timeout", type=float, default=0.05, help="readline 타임아웃(초)")
    parser.add_argument("--list-ports", action="store_true", help="사용 가능한 UART 포트 목록 출력")
    parser.add_argument("--debug", action="store_true", help="UART 수신 raw 라인 출력")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.list_ports:
        print_uart_port_help()
        sys.exit(0)

    stop_event = threading.Event()
    set_uart_debug(args.debug)
    ser = open_uart_serial(args.port, args.baud, args.timeout)
    port_name = ser.port or resolve_uart_port(args.port)

    print(
        f"[ready] port={port_name} baud={args.baud} "
        f"sample={SAMPLE_RATE_HZ}Hz flush={FLUSH_RATE_HZ}Hz batch={SAMPLES_PER_BATCH}"
    )

    reader = threading.Thread(target=read_loop, args=(ser, stop_event), daemon=True)
    flusher = threading.Thread(target=flush_loop, args=(stop_event, None), daemon=True)
    reader.start()
    flusher.start()

    try:
        while reader.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[stop] 종료 중...")
    finally:
        stop_event.set()
        reader.join(timeout=1.0)
        flusher.join(timeout=1.0)
        ser.close()
        remaining = flush_batch()
        if remaining:
            print(f"[stop] 미전송 배치 {len(remaining)} 샘플")


if __name__ == "__main__":
    main()
