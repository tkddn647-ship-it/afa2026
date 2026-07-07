import argparse
import os
import queue
import subprocess
import sys
import threading
import time
from contextlib import suppress
from typing import Any

import cv2
import requests
from requests.adapters import HTTPAdapter
from picamera2 import Picamera2


DEFAULT_CAMERA_URL = "http://3.39.188.80:8012/api/camera/frame"
DEVICE_NAME = "raspberry-pi-camera"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send Raspberry Pi camera frames to the server.")
    parser.add_argument(
        "--url",
        default=DEFAULT_CAMERA_URL,
        help=f"Server camera endpoint. Default: {DEFAULT_CAMERA_URL}",
    )
    parser.add_argument(
        "--device",
        default=DEVICE_NAME,
        help=f"Device/source name shown by the server. Default: {DEVICE_NAME}",
    )
    parser.add_argument("--width", type=int, default=320, help="Frame width. Default: 320")
    parser.add_argument("--height", type=int, default=240, help="Frame height. Default: 240")
    parser.add_argument("--fps", type=float, default=20.0, help="Frames to send per second. Default: 24")
    parser.add_argument("--quality", type=int, default=70, help="JPEG quality from 1 to 100. Default: 100")
    parser.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="Resize frame before sending. Example: 0.75 sends a smaller image. Default: 1.0",
    )
    parser.add_argument("--timeout", type=float, default=0.8, help="HTTP read timeout in seconds. Default: 0.8")
    parser.add_argument("--connect-timeout", type=float, default=0.25, help="HTTP connect timeout in seconds. Default: 0.25")
    parser.add_argument("--workers", type=int, default=4, help="Parallel HTTP sender workers. Default: 4")
    parser.add_argument("--max-pending", type=int, default=2, help="Max frames waiting to send. Default: 2")
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=2.0,
        help="Seconds to wait after a connection error. Default: 2",
    )
    parser.add_argument(
        "--keep-pipewire",
        action="store_true",
        help="pipewire/wireplumber 중지하지 않음 (기본: 카메라 사용 전 자동 중지)",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="rpicam-still 사전 점검 생략",
    )
    parser.add_argument(
        "--stm-ts-file",
        default=None,
        help="최신 STM timestamp(us)가 기록되는 파일 경로. 예: /run/stm_ts_us",
    )
    parser.add_argument(
        "--stm-ts-max-age-ms",
        type=float,
        default=500.0,
        help="stm-ts-file 타임스탬프 허용 최대 age(ms). 기본 500ms",
    )
    parser.add_argument(
        "--require-stm-ts",
        action="store_true",
        help="STM timestamp를 읽지 못하면 해당 프레임 전송을 건너뜀",
    )
    return parser.parse_args()


def encode_jpeg(frame: Any, quality: int) -> bytes:
    safe_quality = max(1, min(100, quality))
    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), safe_quality])
    if not ok:
        raise RuntimeError("failed to encode camera frame as JPEG")
    return encoded.tobytes()


def resize_frame(frame: Any, scale: float) -> Any:
    if scale >= 0.999:
        return frame
    safe_scale = max(0.1, min(1.0, scale))
    return cv2.resize(frame, None, fx=safe_scale, fy=safe_scale, interpolation=cv2.INTER_AREA)


def create_camera(output_width: int, output_height: int) -> tuple[Picamera2, tuple[int, int]]:
    try:
        picam2 = Picamera2()
    except IndexError:
        print("[error] No camera was found by Picamera2.")
        print("[hint] Run this on the Raspberry Pi host, not inside a container without camera devices.")
        print("[hint] If using a container, pass /dev/video*, /dev/media*, and /dev/dma_heap/* through.")
        sys.exit(1)

    # imx708_wide: 센서 네이티브 해상도로 캡처 후 소프트웨어 리사이즈
    config = picam2.create_video_configuration(
        main={"size": (1536, 864), "format": "RGB888"},
        buffer_count=4,
    )
    picam2.configure(config)
    picam2.set_controls({"FrameDurationLimits": (33333, 33333)})
    return picam2, (output_width, output_height)


def put_latest(frame_queue: queue.Queue, payload: dict[str, Any]) -> bool:
    try:
        frame_queue.put_nowait(payload)
        return False
    except queue.Full:
        try:
            frame_queue.get_nowait()
        except queue.Empty:
            pass
        frame_queue.put_nowait(payload)
        return True


def sender_worker(
    worker_id: int,
    args: argparse.Namespace,
    frame_queue: queue.Queue,
    stats: dict[str, int],
    stats_lock: threading.Lock,
    stop_event: threading.Event,
) -> None:
    def new_session() -> requests.Session:
        new = requests.Session()
        adapter = HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=0)
        new.mount("http://", adapter)
        new.mount("https://", adapter)
        return new

    session = new_session()
    while not stop_event.is_set():
        try:
            payload = frame_queue.get(timeout=0.2)
        except queue.Empty:
            continue

        try:
            response = session.post(
                args.url,
                data=payload["data"],
                headers=payload["headers"],
                timeout=(args.connect_timeout, args.timeout),
            )
            response.raise_for_status()
            with stats_lock:
                if stats["sent"] == 0:
                    print(f"[connected] status={response.status_code} mode=binary url={args.url}")
                stats["sent"] += 1
                stats["sent_bytes"] += int(payload["bytes"])
        except requests.exceptions.Timeout as exc:
            with stats_lock:
                stats["errors"] += 1
            print(f"[send timeout] worker={worker_id} reset session {exc}")
            session.close()
            session = new_session()
        except requests.exceptions.ConnectionError as exc:
            with stats_lock:
                stats["errors"] += 1
            print(f"[connection refused] worker={worker_id} {args.url}")
            print("[hint] Start FastAPI on the server with --host 0.0.0.0 and the matching port.")
            print("[hint] Also check the cloud firewall/security group allows that TCP port.")
            print(f"[detail] {exc}")
            session.close()
            session = new_session()
            time.sleep(max(args.retry_delay, 0.1))
        except Exception as exc:
            with stats_lock:
                stats["errors"] += 1
            print(f"[send error] worker={worker_id} {exc}")
            session.close()
            session = new_session()
        finally:
            frame_queue.task_done()


def _camera_device_busy() -> list[str]:
    busy: list[str] = []
    for path in ("/dev/video4", "/dev/media0"):
        try:
            result = subprocess.run(
                ["fuser", path],
                capture_output=True,
                text=True,
                check=False,
            )
            for line in result.stderr.splitlines():
                if "pipewire" in line or "wireplumber" in line:
                    busy.append("pipewire")
                elif line.strip() and line.split()[-1].isdigit():
                    name = line.split()[-2] if len(line.split()) >= 2 else "unknown"
                    if name not in busy and name not in ("USER", "COMMAND"):
                        busy.append(name)
        except OSError:
            pass
    return list(dict.fromkeys(busy))


def release_camera_devices(keep_pipewire: bool) -> None:
    if keep_pipewire:
        busy = _camera_device_busy()
        if busy:
            print(f"[warn] 카메라 장치 사용 중: {', '.join(busy)}")
            print("       --keep-pipewire 없이 실행하거나 wireplumber 설정 확인")
        return

    busy = _camera_device_busy()
    if not busy:
        return

    print("[info] pipewire/wireplumber 가 카메라를 점유 중 → 오디오 서비스 잠시 중지")
    subprocess.run(
        ["systemctl", "--user", "stop", "wireplumber", "pipewire", "pipewire-pulse"],
        check=False,
    )
    for _ in range(20):
        if not _camera_device_busy():
            print("[info] 카메라 장치 해제됨")
            return
        time.sleep(0.15)
    print("[error] 카메라 장치가 여전히 사용 중입니다.")
    print("       systemctl --user stop wireplumber pipewire pipewire-pulse 후 재시도")
    sys.exit(1)


def warn_if_camera_busy() -> None:
    try:
        result = subprocess.run(
            ["fuser", "-v", "/dev/video4"],
            capture_output=True,
            text=True,
            check=False,
        )
        holders = [
            line.strip()
            for line in result.stderr.splitlines()
            if "pipewire" in line or "wireplumber" in line
        ]
        if holders:
            print("[warn] pipewire/wireplumber 이 카메라 장치를 사용 중일 수 있습니다.")
            print("       테스트 전: systemctl --user stop wireplumber pipewire pipewire-pulse")
    except OSError:
        pass


def read_stm_ts_us(args: argparse.Namespace, state: dict[str, Any]) -> int | None:
    path = args.stm_ts_file
    if not path:
        return None
    try:
        stat = os.stat(path)
        age_ms = (time.time() - stat.st_mtime) * 1000.0
        if age_ms > max(args.stm_ts_max_age_ms, 0.0):
            now = time.monotonic()
            if now - state["last_stale_warn"] >= 1.0:
                print(f"[warn] stale STM timestamp file age={age_ms:.0f}ms path={path}")
                state["last_stale_warn"] = now
            return None
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return None
        return int(raw.split()[0])
    except (OSError, ValueError):
        return None


def kill_stale_camera_clients() -> None:
    """이전에 죽지 않은 rpicam/camera.py 가 카메라를 붙잡는 경우 정리."""
    subprocess.run(
        ["pkill", "-9", "-f", "rpicam-still|rpicam-hello|rpicam-vid"],
        check=False,
    )
    time.sleep(0.3)


def preflight_hardware_test() -> bool:
    """공식 rpicam-still 으로 하드웨어 캡처 가능 여부 확인."""
    test_path = "/tmp/camera_preflight.jpg"
    with suppress(OSError):
        os.remove(test_path)
    result = subprocess.run(
        [
            "rpicam-still",
            "-o",
            test_path,
            "-n",
            "-t",
            "1",
            "--width",
            "1536",
            "--height",
            "864",
        ],
        capture_output=True,
        text=True,
        timeout=12,
        check=False,
    )
    if os.path.isfile(test_path) and os.path.getsize(test_path) > 1000:
        print(f"[ok] 하드웨어 캡처 성공 ({os.path.getsize(test_path)} bytes)")
        return True
    tail = (result.stderr or result.stdout or "").strip().splitlines()
    for line in tail[-4:]:
        if line.strip():
            print(f"  {line}")
    print("[error] 카메라 하드웨어 캡처 실패 (camera.py 문제 아님)")

    combined = (result.stderr or "") + (result.stdout or "")
    if "no cameras available" in combined.lower():
        print("  커널: imx708 칩 ID 읽기 실패(error -5) → 케이블 접촉 불량 가능성 큼")
        print("  1) 전원 OFF → CSI 케이블 양쪽 완전히 다시 꽂기")
        print("  2) CAM0 <-> CAM1 포트 바꿔보기")
        print("  3) Pi5 전용 FPC 케이블인지 확인")
        print("  4) sudo reboot 후: rpicam-hello --list-cameras")
    else:
        print("  카메라는 인식되나 프레임 캡처 타임아웃 → CSI 데이터선 문제")
        print("  1) CSI 케이블 양쪽 다시 꽂기 (클립 완전히 잠금)")
        print("  2) Pi5 다른 포트 CAM0 <-> CAM1 바꿔보기")
        print("  3) 5V 5A 전원 어댑터 사용")
        print("  4) sudo reboot 후: rpicam-still -o test.jpg -n -t 1")
    return False


def main() -> None:
    args = parse_args()
    kill_stale_camera_clients()
    release_camera_devices(args.keep_pipewire)
    if not getattr(args, "skip_preflight", False):
        if not preflight_hardware_test():
            sys.exit(1)
    frame_period = 1.0 / max(args.fps, 0.1)

    picam2, output_size = create_camera(args.width, args.height)
    print(f"[camera] capture 1536x864 -> send {args.width}x{args.height} at {args.fps:g} fps")
    print(f"[server] {args.url}")
    print(f"[sender] workers={args.workers} max_pending={args.max_pending}")
    picam2.start()
    time.sleep(1.0)

    frame_queue: queue.Queue = queue.Queue(maxsize=max(1, args.max_pending))
    stats = {"captured": 0, "sent": 0, "sent_bytes": 0, "dropped": 0, "errors": 0}
    stats_lock = threading.Lock()
    stop_event = threading.Event()
    for worker_id in range(max(1, args.workers)):
        thread = threading.Thread(
            target=sender_worker,
            args=(worker_id + 1, args, frame_queue, stats, stats_lock, stop_event),
            daemon=True,
        )
        thread.start()

    last_sent = 0
    last_sent_bytes = 0
    last_captured = 0
    next_seq = 0
    session_id = str(int(time.time() * 1000))
    window_started = time.monotonic()
    stm_state: dict[str, Any] = {"last_stale_warn": 0.0}
    try:
        while True:
            started = time.monotonic()
            frame = picam2.capture_array()
            frame = cv2.resize(frame, output_size, interpolation=cv2.INTER_AREA)
            frame = resize_frame(frame, args.scale)
            height, width = frame.shape[:2]
            frame_jpeg = encode_jpeg(frame, args.quality)
            next_seq += 1
            capture_ts_us = int(time.time() * 1_000_000)
            stm_ts_us = read_stm_ts_us(args, stm_state)
            if args.require_stm_ts and stm_ts_us is None:
                with stats_lock:
                    stats["dropped"] += 1
                    stats["captured"] += 1
                continue

            payload: dict[str, Any] = {
                "data": frame_jpeg,
                "bytes": len(frame_jpeg),
                "headers": {
                    "Content-Type": "image/jpeg",
                    "X-Device": args.device,
                    "X-Source": args.device,
                    "X-Frame-Width": str(width),
                    "X-Frame-Height": str(height),
                    "X-Frame-Seq": str(next_seq),
                    "X-Frame-Session": session_id,
                    "X-Frame-Capture-Ts-Us": str(capture_ts_us),
                    "Cache-Control": "no-store",
                },
            }
            if stm_ts_us is not None:
                payload["headers"]["X-Frame-Stm-Ts-Us"] = str(stm_ts_us)

            dropped = put_latest(frame_queue, payload)
            with stats_lock:
                stats["captured"] += 1
                if dropped:
                    stats["dropped"] += 1

            now = time.monotonic()
            if now - window_started >= 1.0:
                with stats_lock:
                    captured = stats["captured"]
                    sent = stats["sent"]
                    sent_bytes = stats["sent_bytes"]
                    dropped_total = stats["dropped"]
                    errors = stats["errors"]
                elapsed_window = now - window_started
                capture_fps = (captured - last_captured) / elapsed_window
                send_fps = (sent - last_sent) / elapsed_window
                send_mbps = ((sent_bytes - last_sent_bytes) * 8.0 / 1_000_000.0) / elapsed_window
                print(
                    f"[fps] capture={capture_fps:.1f} send={send_fps:.1f} "
                    f"mbps={send_mbps:.2f} sent={sent} dropped={dropped_total} "
                    f"errors={errors} queued={frame_queue.qsize()}"
                )
                last_captured = captured
                last_sent = sent
                last_sent_bytes = sent_bytes
                window_started = now

            elapsed = time.monotonic() - started
            sleep_for = frame_period - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        print("\n[stop] interrupted")
    except Exception as exc:
        print(f"\n[error] 캡처 실패: {exc}")
        print("[hint] CSI 케이블·포트·전원 확인 후 rpicam-still -o test.jpg -n -t 1")
        sys.exit(1)
    finally:
        stop_event.set()
        picam2.stop()


if __name__ == "__main__":
    main()
