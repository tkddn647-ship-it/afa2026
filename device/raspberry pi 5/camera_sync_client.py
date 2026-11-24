"""Pi → server outbound camera sync command client.

카메라 코드가 실제 첫 인코딩 프레임을 확인한 뒤 report()를 호출해야 한다.
이 모듈은 서버 수신 시각이나 HTTP 호출 시각으로 프레임 시각을 생성하지 않는다.
"""

from __future__ import annotations

import os
from typing import Any

import requests


class CameraSyncClient:
    def __init__(
        self,
        *,
        server_url: str | None = None,
        token: str | None = None,
        device: str | None = None,
        timeout_sec: float | None = None,
    ) -> None:
        self.server_url = (
            server_url or os.getenv("CAMERA_SYNC_SERVER_URL", "")
        ).strip().rstrip("/")
        self.token = (token or os.getenv("CAMERA_SYNC_SERVER_TOKEN", "")).strip()
        self.device = (
            device
            or os.getenv("CAMERA_SYNC_DEVICE", "raspberry-pi-camera")
        ).strip()
        self.timeout_sec = float(
            timeout_sec
            if timeout_sec is not None
            else os.getenv("CAMERA_SYNC_HTTP_TIMEOUT", "5")
        )
        if not self.server_url:
            raise ValueError("CAMERA_SYNC_SERVER_URL이 필요합니다.")
        if not self.token:
            raise ValueError("CAMERA_SYNC_SERVER_TOKEN이 필요합니다.")
        if not self.device:
            raise ValueError("CAMERA_SYNC_DEVICE가 필요합니다.")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            }
        )

    def next_command(self, pi_boot_id: str) -> dict[str, str] | None:
        response = self.session.get(
            f"{self.server_url}/api/sync/commands/next",
            params={"device": self.device, "pi_boot_id": pi_boot_id},
            timeout=self.timeout_sec,
        )
        if response.status_code == 204:
            return None
        response.raise_for_status()
        payload = response.json()
        command = {
            key: str(payload.get(key) or "").strip()
            for key in ("command_id", "recording_id", "session_id")
        }
        if not all(command.values()):
            raise ValueError("서버 sync command 응답 필드가 불완전합니다.")
        return command

    def report(
        self,
        *,
        command: dict[str, str],
        pi_boot_id: str,
        first_frame_pi_mono_ns: int,
        first_frame_pts_ns: int,
        stream_first_sensor_mono_ns: int,
        camera_clock_source: str,
        rtp_timestamp: int | None,
        reported_pi_mono_ns: int,
        reported_pi_realtime_ns: int,
        rtp_clock_rate: int = 90000,
    ) -> dict[str, Any]:
        payload = {
            "device": self.device,
            "command_id": command["command_id"],
            "recording_id": command["recording_id"],
            "session_id": command["session_id"],
            "pi_boot_id": pi_boot_id,
            "first_frame_pi_mono_ns": int(first_frame_pi_mono_ns),
            "first_frame_pts_ns": int(first_frame_pts_ns),
            "stream_first_sensor_mono_ns": int(stream_first_sensor_mono_ns),
            "camera_clock_source": str(camera_clock_source),
            "rtp_timestamp": (
                None if rtp_timestamp is None else int(rtp_timestamp) & 0xFFFF_FFFF
            ),
            "rtp_clock_rate": int(rtp_clock_rate),
            "reported_pi_mono_ns": int(reported_pi_mono_ns),
            "reported_pi_realtime_ns": int(reported_pi_realtime_ns),
        }
        response = self.session.post(
            f"{self.server_url}/api/sync/report",
            json=payload,
            timeout=self.timeout_sec,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("서버 sync report 응답이 JSON object가 아닙니다.")
        return result
