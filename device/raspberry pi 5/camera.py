import argparse
import queue
import sys
import threading
import time
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
    parser.add_argument("--fps", type=float, default=24.0, help="Frames to send per second. Default: 24")
    parser.add_argument("--quality", type=int, default=35, help="JPEG quality from 1 to 100. Default: 35")
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


def create_camera(width: int, height: int) -> Picamera2:
    try:
        picam2 = Picamera2()
    except IndexError:
        print("[error] No camera was found by Picamera2.")
        print("[hint] Run this on the Raspberry Pi host, not inside a container without camera devices.")
        print("[hint] If using a container, pass /dev/video*, /dev/media*, and /dev/dma_heap/* through.")
        sys.exit(1)
    except RuntimeError as exc:
        print(f"[error] Failed to open camera: {exc}")
        print("[hint] The camera is probably already in use by another process.")
        print("[hint] Check with: ps -ef | grep -E 'camera|libcamera|rpicam|picamera|python'")
        print("[hint] Check devices with: sudo fuser -v /dev/video* /dev/media*")
        print("[hint] Stop the process using the camera, then run this script again.")
        sys.exit(1)

    config = picam2.create_video_configuration(
        main={
            "size": (width, height),
            "format": "RGB888",
        }
    )
    picam2.configure(config)
    return picam2


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


def main() -> None:
    args = parse_args()
    frame_period = 1.0 / max(args.fps, 0.1)

    picam2 = create_camera(args.width, args.height)
    print(f"[camera] starting {args.width}x{args.height} at {args.fps:g} fps")
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
    try:
        while True:
            started = time.monotonic()
            frame = resize_frame(picam2.capture_array(), args.scale)
            height, width = frame.shape[:2]
            frame_jpeg = encode_jpeg(frame, args.quality)
            next_seq += 1

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
                    "Cache-Control": "no-store",
                },
            }

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
    finally:
        stop_event.set()
        picam2.stop()


if __name__ == "__main__":
    main()
