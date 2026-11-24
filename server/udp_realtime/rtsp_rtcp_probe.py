from __future__ import annotations

import re
import socket
import struct
import threading
from contextlib import suppress
from typing import Any
from urllib.parse import urljoin, urlparse


NTP_UNIX_EPOCH_DELTA = 2_208_988_800


def parse_rtcp_sender_reports(packet: bytes) -> list[dict[str, int]]:
    reports: list[dict[str, int]] = []
    offset = 0
    while offset + 4 <= len(packet):
        first, packet_type, length_words = struct.unpack_from("!BBH", packet, offset)
        version = first >> 6
        packet_bytes = (length_words + 1) * 4
        if version != 2 or packet_bytes < 4 or offset + packet_bytes > len(packet):
            break
        if packet_type == 200 and packet_bytes >= 28:
            ssrc, ntp_seconds, ntp_fraction, rtp_timestamp = struct.unpack_from(
                "!IIII", packet, offset + 4
            )
            ntp_unix_ns = (
                (ntp_seconds - NTP_UNIX_EPOCH_DELTA) * 1_000_000_000
                + (ntp_fraction * 1_000_000_000 // (1 << 32))
            )
            reports.append(
                {
                    "ssrc": ssrc,
                    "rtp_timestamp": rtp_timestamp,
                    "ntp_seconds": ntp_seconds,
                    "ntp_fraction": ntp_fraction,
                    "ntp_unix_ns": ntp_unix_ns,
                }
            )
        offset += packet_bytes
    return reports


class RtspRtcpProbe:
    """Short-lived RTSP TCP reader that captures one RTCP Sender Report."""

    def __init__(self, rtsp_url: str, *, timeout_sec: float = 12.0) -> None:
        self.rtsp_url = rtsp_url
        self.timeout_sec = max(2.0, float(timeout_sec))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self.result: dict[str, Any] = {
            "status": "pending",
            "rtp_clock_rate": 90000,
        }

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="rtsp-rtcp-probe",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        sock = self._socket
        if sock is not None:
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                sock.close()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.result)

    def _set_result(self, **values: Any) -> None:
        with self._lock:
            self.result.update(values)

    @staticmethod
    def _read_response(stream: Any) -> tuple[int, dict[str, str], bytes]:
        status_line = stream.readline()
        if not status_line:
            raise OSError("RTSP response가 없습니다.")
        match = re.match(rb"RTSP/\d\.\d\s+(\d+)", status_line)
        if not match:
            raise OSError(f"잘못된 RTSP response: {status_line[:100]!r}")
        headers: dict[str, str] = {}
        while True:
            line = stream.readline()
            if line in (b"", b"\r\n", b"\n"):
                break
            key, separator, value = line.decode(
                "utf-8", errors="replace"
            ).partition(":")
            if separator:
                headers[key.strip().lower()] = value.strip()
        content_length = int(headers.get("content-length", "0") or 0)
        body = stream.read(content_length) if content_length else b""
        return int(match.group(1)), headers, body

    def _request(
        self,
        stream: Any,
        method: str,
        url: str,
        cseq: int,
        *,
        session: str = "",
        headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, str], bytes]:
        request_headers = {
            "CSeq": str(cseq),
            "User-Agent": "afa-rtcp-probe/1",
        }
        if session:
            request_headers["Session"] = session
        request_headers.update(headers or {})
        raw = (
            f"{method} {url} RTSP/1.0\r\n"
            + "".join(f"{key}: {value}\r\n" for key, value in request_headers.items())
            + "\r\n"
        ).encode()
        stream.write(raw)
        stream.flush()
        status, response_headers, body = self._read_response(stream)
        if status < 200 or status >= 300:
            raise OSError(f"RTSP {method} failed: {status}")
        return response_headers, body

    def _run(self) -> None:
        try:
            parsed = urlparse(self.rtsp_url)
            if parsed.scheme.lower() != "rtsp" or not parsed.hostname:
                raise ValueError("유효한 RTSP URL이 아닙니다.")
            port = parsed.port or 554
            sock = socket.create_connection(
                (parsed.hostname, port),
                timeout=self.timeout_sec,
            )
            self._socket = sock
            sock.settimeout(self.timeout_sec)
            stream = sock.makefile("rwb", buffering=0)

            describe_headers, sdp_bytes = self._request(
                stream,
                "DESCRIBE",
                self.rtsp_url,
                1,
                headers={"Accept": "application/sdp"},
            )
            sdp = sdp_bytes.decode("utf-8", errors="replace")
            control = ""
            for line in sdp.splitlines():
                if line.startswith("a=control:") and line[10:].strip() != "*":
                    control = line[10:].strip()
                    break
            if not control:
                raise OSError("SDP video control URL을 찾을 수 없습니다.")
            base_url = (
                describe_headers.get("content-base")
                or describe_headers.get("content-location")
                or self.rtsp_url.rstrip("/") + "/"
            )
            track_url = (
                control
                if control.lower().startswith("rtsp://")
                else urljoin(base_url, control)
            )
            setup_headers, _ = self._request(
                stream,
                "SETUP",
                track_url,
                2,
                headers={
                    "Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"
                },
            )
            session = setup_headers.get("session", "").split(";", 1)[0]
            transport = setup_headers.get("transport", "")
            ssrc_match = re.search(r"ssrc=([0-9A-Fa-f]+)", transport)
            setup_ssrc = int(ssrc_match.group(1), 16) if ssrc_match else None
            channel_match = re.search(r"interleaved=(\d+)-(\d+)", transport)
            rtcp_channel = int(channel_match.group(2)) if channel_match else 1

            play_headers, _ = self._request(
                stream,
                "PLAY",
                self.rtsp_url,
                3,
                session=session,
                headers={"Range": "npt=0.000-"},
            )
            self._set_result(
                status="waiting_rtcp_sr",
                setup_ssrc=setup_ssrc,
                rtp_info=play_headers.get("rtp-info", ""),
            )

            while not self._stop.is_set():
                marker = stream.read(1)
                if not marker:
                    break
                if marker != b"$":
                    stream.readline()
                    continue
                header = stream.read(3)
                if len(header) != 3:
                    break
                channel = header[0]
                size = struct.unpack("!H", header[1:])[0]
                payload = stream.read(size)
                if len(payload) != size or channel != rtcp_channel:
                    continue
                reports = parse_rtcp_sender_reports(payload)
                if reports:
                    self._set_result(status="ready", **reports[0])
                    return
            if self.snapshot().get("status") != "ready":
                self._set_result(status="unavailable", reason="RTCP SR 미수신")
        except Exception as exc:
            self._set_result(
                status="unavailable",
                reason=f"{type(exc).__name__}: {exc}",
            )
        finally:
            sock = self._socket
            if sock is not None:
                with suppress(OSError):
                    sock.close()
            self._socket = None
