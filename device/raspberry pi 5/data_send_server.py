"""
STM32 UART 수신 배치를 app.py /ingest 로 20 Hz 전송.

Uart_stm.py 로 UART 수신 후, 50 ms 배치(최대 10샘플)를 HTTP POST.
서버는 samples 배열을 200 Hz 간격(5 ms)으로 CSV에 기록합니다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

from Uart_stm import (
    DEFAULT_BAUD,
    DEFAULT_UART_PORT,
    FLUSH_RATE_HZ,
    SAMPLE_RATE_HZ,
    UART_MODULE_VERSION,
    flush_batch,
    flush_loop,
    open_uart_serial,
    print_uart_port_help,
    read_loop,
    set_uart_debug,
    uart_stats,
)

SCRIPT_DIR = Path(__file__).resolve().parent


def _load_env_file(filename: str = "stm_ingest.env") -> None:
    for env_path in (SCRIPT_DIR / filename, Path.home() / filename):
        if not env_path.is_file():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        print(f"[info] env 로드: {env_path}")
        return


_load_env_file()

DEFAULT_INGEST_URL = os.getenv("INGEST_URL", "http://3.39.188.80:8000/ingest")
DEFAULT_REALTIME_URL = os.getenv("REALTIME_URL", "").strip()
DEFAULT_DEVICE = os.getenv("DEVICE", "raspberry-pi-stm")
DEFAULT_UART_PORT_ENV = os.getenv("UART_PORT", DEFAULT_UART_PORT)
SCRIPT_VERSION = "2026-06-24-lws-steering"
STATUS_INTERVAL_S = 2.0


def sample_to_server_dict(sample: dict[str, Any]) -> dict[str, Any]:
    stm_ms = sample.get("stm_ms")
    if stm_ms is None:
        stm_ms = int(float(sample.get("timestamp", time.time())) * 1000)

    mcu_temp = round(float(sample.get("ecu_temp", 0)), 1)
    steer_angle = round(float(sample.get("steering_angle", 0)), 1)
    steer_speed = round(float(sample.get("steering_speed", 0)), 0)

    return {
        "t": int(stm_ms),
        "ecu_temp": mcu_temp,
        "steering_angle": steer_angle,
        "steering_speed": steer_speed,
        "steering": {
            "angle": steer_angle,
            "speed": steer_speed,
        },
        "linear": {
            "fr": round(float(sample["FR"]), 2),
            "fl": round(float(sample["FL"]), 2),
            "rr": round(float(sample["RR"]), 2),
            "rl": round(float(sample["RL"]), 2),
        },
        "accel": {
            "x": round(float(sample["x_g"]), 3),
            "y": round(float(sample["y_g"]), 3),
            "z": round(float(sample["z_g"]), 3),
        },
    }


def build_ingest_payload(batch: list[dict[str, Any]], device: str) -> dict[str, Any]:
    return {
        "device": device,
        "samples": [sample_to_server_dict(sample) for sample in batch],
    }


def stats_url_from_ingest(ingest_url: str) -> str:
    parsed = urlparse(ingest_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    return f"{base}/api/stats"


def probe_ingest_server(ingest_url: str, timeout: float) -> bool:
    stats_url = stats_url_from_ingest(ingest_url)
    try:
        response = requests.get(stats_url, timeout=timeout)
        response.raise_for_status()
        print(f"[info] 서버 연결 OK: {stats_url}")
        return True
    except requests.RequestException as exc:
        print(f"[error] 서버 연결 실패: {stats_url} ({exc})")
        print("[hint] app.py 가 실행 중인지, --url 포트가 맞는지 확인")
        return False


class BatchSender:
    def __init__(
        self,
        url: str,
        device: str,
        timeout: float,
        realtime_url: str = "",
    ) -> None:
        self.url = url
        self.realtime_url = realtime_url.strip()
        self.device = device
        self.timeout = timeout
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.sent_batches = 0
        self.sent_samples = 0
        self.failed_batches = 0
        self.realtime_sent_batches = 0
        self.realtime_failed_batches = 0
        self.last_error = ""
        self.last_http_status = 0

    def _post_json(self, url: str, body: dict[str, Any]) -> None:
        response = self.session.post(
            url,
            data=json.dumps(body, ensure_ascii=False),
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        self.last_http_status = response.status_code
        response.raise_for_status()

    def send(self, batch: list[dict[str, Any]]) -> bool:
        if not batch:
            return True

        body = build_ingest_payload(batch, self.device)
        try:
            self._post_json(self.url, body)
        except requests.RequestException as exc:
            self.failed_batches += 1
            self.last_error = str(exc)
            print(f"[error] ingest 실패 ({self.url}): {exc}")
            return False

        self.sent_batches += 1
        self.sent_samples += len(batch)
        self.last_error = ""

        if self.realtime_url:
            try:
                self._post_json(self.realtime_url, body)
                self.realtime_sent_batches += 1
            except requests.RequestException as exc:
                self.realtime_failed_batches += 1
                print(f"[warn] realtime 실패 ({self.realtime_url}): {exc}")

        return True


def on_batch_factory(sender: BatchSender) -> Any:
    def on_batch(batch: list[dict[str, Any]]) -> None:
        ok = sender.send(batch)
        if ok:
            last = batch[-1]
            steer = float(last.get("steering_angle", 0))
            steer_spd = float(last.get("steering_speed", 0))
            print(
                f"[sent] n={len(batch)} total={sender.sent_samples} "
                f"FR={last['FR']:.2f} x_g={last['x_g']:.3f} "
                f"mcu={last.get('ecu_temp', 0):.1f}C "
                f"steer={steer:.1f}°/{steer_spd:.0f}°/s"
            )

    return on_batch


def print_runtime_status(sender: BatchSender, baud: int) -> None:
    rx = uart_stats.snapshot()
    rt_part = ""
    if sender.realtime_url:
        rt_part = (
            f" rt_ok={sender.realtime_sent_batches} "
            f"rt_fail={sender.realtime_failed_batches}"
        )
    print(
        f"[status] uart_lines={rx['raw_lines']} parsed={rx['parsed_samples']} "
        f"parse_err={rx['parse_errors']} sent_batches={sender.sent_batches} "
        f"sent_samples={sender.sent_samples} ingest_fail={sender.failed_batches}"
        f"{rt_part}"
    )
    if rx["raw_lines"] == 0:
        print("[warn] STM UART 데이터 없음")
        print(f"       STM 플래시 확인, 배선(TX↔RX), 보드레이트 {baud} 일치, GND 공통")
        print("       테스트: python data_send_server.py --debug")
    elif rx["parsed_samples"] == 0:
        print("[warn] UART 수신은 되나 CSV 파싱 실패 (보드레이트 불일치 가능)")
        if rx["last_bad_line"]:
            print(f"       bad: {rx['last_bad_line']}")
        print("       다른 보드레이트 시도: --baud 115200")
    elif sender.sent_samples == 0 and sender.failed_batches > 0:
        print(f"[warn] ingest 전송 실패: {sender.last_error}")
    elif sender.sent_samples == 0:
        print("[warn] 파싱은 됐으나 아직 서버 전송 없음 (50ms 배치 대기 중)")


def status_loop(stop_event: threading.Event, sender: BatchSender, baud: int) -> None:
    next_status = time.monotonic() + STATUS_INTERVAL_S
    while not stop_event.is_set():
        now = time.monotonic()
        if now >= next_status:
            print_runtime_status(sender, baud)
            next_status += STATUS_INTERVAL_S
        stop_event.wait(0.2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="STM32 UART -> app.py /ingest (20 Hz)")
    parser.add_argument("--url", default=DEFAULT_INGEST_URL, help=f"ingest URL. Default: {DEFAULT_INGEST_URL}")
    parser.add_argument(
        "--realtime-url",
        default=DEFAULT_REALTIME_URL,
        help="선택: realtime_server /api/live/ingest URL (동시 전송)",
    )
    parser.add_argument("--device", default=DEFAULT_DEVICE, help=f"device 이름. Default: {DEFAULT_DEVICE}")
    parser.add_argument("--port", default=DEFAULT_UART_PORT_ENV, help="UART 디바이스 (auto=자동탐지)")
    parser.add_argument("--baud", type=int, default=int(os.getenv("UART_BAUD", str(DEFAULT_BAUD))), help="UART 보드레이트")
    parser.add_argument("--timeout", type=float, default=float(os.getenv("HTTP_TIMEOUT", "0.8")), help="HTTP 타임아웃(초)")
    parser.add_argument("--uart-timeout", type=float, default=0.05, help="UART readline 타임아웃(초)")
    parser.add_argument("--list-ports", action="store_true", help="사용 가능한 UART 포트 목록 출력")
    parser.add_argument("--debug", action="store_true", help="UART raw 라인 출력")
    parser.add_argument("--skip-probe", action="store_true", help="시작 시 서버 연결 확인 생략")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.list_ports:
        print_uart_port_help()
        sys.exit(0)

    if not args.skip_probe and not probe_ingest_server(args.url, args.timeout):
        sys.exit(1)

    stop_event = threading.Event()
    set_uart_debug(args.debug)
    sender = BatchSender(args.url, args.device, args.timeout, args.realtime_url)
    ser = open_uart_serial(args.port, args.baud, args.uart_timeout)
    port_name = ser.port or args.port
    print(f"[info] data_send_server {SCRIPT_VERSION} + uart_stm {UART_MODULE_VERSION}")
    print(
        f"[ready] uart={port_name}@{args.baud} "
        f"ingest={args.url} device={args.device} "
        f"sample={SAMPLE_RATE_HZ}Hz send={FLUSH_RATE_HZ}Hz"
    )
    if args.realtime_url:
        print(f"[ready] realtime={args.realtime_url}")
    print("[info] 2초마다 status 출력. 데이터 없으면 [warn] 확인")

    on_batch = on_batch_factory(sender)
    reader = threading.Thread(target=read_loop, args=(ser, stop_event), daemon=True)
    flusher = threading.Thread(target=flush_loop, args=(stop_event, on_batch), daemon=True)
    monitor = threading.Thread(target=status_loop, args=(stop_event, sender, args.baud), daemon=True)
    reader.start()
    flusher.start()
    monitor.start()

    try:
        while reader.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[stop] 종료 중...")
    finally:
        stop_event.set()
        reader.join(timeout=1.0)
        flusher.join(timeout=1.0)
        monitor.join(timeout=1.0)
        ser.close()
        remaining = flush_batch(on_batch)
        if remaining:
            print(f"[stop] 마지막 배치 {len(remaining)} 샘플 전송")
        print_runtime_status(sender, args.baud)


if __name__ == "__main__":
    main()
