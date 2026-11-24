from __future__ import annotations

import asyncio
import atexit
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SERVER_DIR = HERE.parent
ANALYSIS_DIR = SERVER_DIR / "analysis_server"
REALTIME_DIR = SERVER_DIR / "udp_realtime"
for path in (str(HERE), str(SERVER_DIR), str(ANALYSIS_DIR), str(REALTIME_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

TEST_APP_DB = Path(tempfile.gettempdir()) / f"afa-ingest-v2-test-{os.getpid()}.sqlite3"
os.environ["INGEST_V2_DB_PATH"] = str(TEST_APP_DB)


def _remove_test_app_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(f"{TEST_APP_DB}{suffix}").unlink()
        except FileNotFoundError:
            pass


atexit.register(_remove_test_app_db)

import app as logger_app
import analysis as analysis_app
from camera_sync_store import CameraSyncStore
from ingest_v2 import IngestV2Store, V2ValidationError, validate_v2_payload
from sync_timeline import rtp_to_ntp_ns, video_pts_to_pi_mono_ns
from rtsp_rtcp_probe import NTP_UNIX_EPOCH_DELTA, parse_rtcp_sender_reports


def v2_payload(seq: int = 10) -> dict:
    return {
        "schema_version": 2,
        "device": "pi-stm",
        "pi_boot_id": "boot-a",
        "sender_instance_id": "sender-a",
        "batch_seq": seq,
        "uart_rx_mono_ns": 5_000_000_000 + seq,
        "pi_clock_source": "CLOCK_BOOTTIME",
        "first_sample_stm_ms": seq * 10,
        "last_sample_stm_ms": seq * 10 + 5,
        "sample_count": 2,
        "stm_epoch": 3,
        "stm_wrap_count": 1,
        "stm_clock_anchor": {"stm_extended_ms": 100, "pi_mono_ns": 200},
        "stm_clock_model": {"slope_ns_per_ms": 1_000_000},
        "samples": [
            {
                "t": seq * 10,
                "stm_extended_ms": seq * 10,
                "pi_mono_fixed_5ms_ns": 1_000_000_000 + seq,
                "pi_mono_est_ns": 1_100_000_000 + seq,
                "linear": {"fr": 1, "fl": 2, "rr": 3, "rl": 4},
                "accel": {"x": 0.1, "y": 0.2, "z": 1.0},
            },
            {
                "t": seq * 10 + 5,
                "stm_extended_ms": seq * 10 + 5,
                "pi_mono_fixed_5ms_ns": 1_005_000_000 + seq,
                "pi_mono_est_ns": 1_105_000_000 + seq,
                "linear": {"fr": 5, "fl": 6, "rr": 7, "rl": 8},
                "accel": {"x": 0.3, "y": 0.4, "z": 0.9},
            },
        ],
    }


class DummyRequest:
    def __init__(self, body: dict | None = None, *, token: str = "") -> None:
        self._body = body
        self.client = SimpleNamespace(host="127.0.0.1", port=1234)
        self.headers: dict[str, str] = (
            {"authorization": f"Bearer {token}"} if token else {}
        )

    async def json(self) -> dict | None:
        return self._body


class IngestV2Tests(unittest.TestCase):
    def test_validation_rejects_count_and_last_timestamp_mismatch(self) -> None:
        body = v2_payload()
        body["sample_count"] = 3
        with self.assertRaises(V2ValidationError):
            validate_v2_payload(body)
        body = v2_payload()
        body["last_sample_stm_ms"] += 1
        with self.assertRaises(V2ValidationError):
            validate_v2_payload(body)

    def test_dedup_survives_store_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "v2.sqlite3"
            body = v2_payload()
            validated = validate_v2_payload(body)
            first = IngestV2Store(path).register_batch(body, validated)
            second = IngestV2Store(path).register_batch(body, validated)
            self.assertTrue(first["accepted"])
            self.assertTrue(second["duplicate"])

    def test_http_duplicate_returns_200_and_is_not_processed_twice(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = IngestV2Store(Path(temp) / "v2.sqlite3")
            with (
                patch.object(logger_app, "ingest_v2_store", store),
                patch.object(logger_app, "schedule_live_processing") as live,
            ):
                first = asyncio.run(logger_app.ingest_http(DummyRequest(v2_payload())))
                second = asyncio.run(logger_app.ingest_http(DummyRequest(v2_payload())))
            self.assertFalse(first["duplicate"])
            self.assertTrue(second["duplicate"])
            self.assertEqual(live.call_count, 1)

    def test_missing_ranges_and_out_of_order_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = IngestV2Store(Path(temp) / "v2.sqlite3")
            for seq in (10, 12):
                body = v2_payload(seq)
                store.register_batch(body, validate_v2_payload(body))
            status = store.sequence_status("pi-stm", "boot-a")
            self.assertEqual(status["missing_ranges"], [{"start": 11, "end": 11, "count": 1}])

            body = v2_payload(11)
            result = store.register_batch(body, validate_v2_payload(body))
            self.assertTrue(result["out_of_order"])
            self.assertEqual(store.sequence_status("pi-stm", "boot-a")["missing_ranges"], [])
            self.assertEqual(
                [item["batch_seq"] for item in store.ordered_batch_metadata("pi-stm", "boot-a")],
                [10, 11, 12],
            )

    def test_analysis_restores_v2_rows_by_batch_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            csv_path = Path(temp) / "out-of-order.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "timestamp,pi_boot_id,batch_seq,stm_extended_ms,pi_mono_est_ns,FR",
                        "2026/07/21 00:00:00.000,boot-a,12,120,1010000000,12",
                        "2026/07/21 00:00:00.001,boot-a,10,100,1000000000,10",
                        "2026/07/21 00:00:00.002,boot-a,11,110,1005000000,11",
                    ]
                ),
                encoding="utf-8",
            )
            parsed = analysis_app._load_csv(csv_path)
        self.assertEqual(parsed["series"]["FR"], [10.0, 11.0, 12.0])
        self.assertEqual(parsed["time_source"], "pi_mono_est_ns_batch_seq_order")
        self.assertAlmostEqual(parsed["duration_sec"], 0.01, places=6)

    def test_legacy_and_v2_csv_rows_are_compatible(self) -> None:
        legacy = {
            "time": "2026-07-21T00:00:00+00:00",
            "ip": "127.0.0.1",
            "port": 1,
            "message": json.dumps(
                {
                    "device": "legacy",
                    "samples": [
                        {
                            "t": 5,
                            "linear": {"fr": 1, "fl": 2, "rr": 3, "rl": 4},
                            "accel": {"x": 0, "y": 0, "z": 1},
                        }
                    ],
                }
            ),
        }
        legacy_row = logger_app.build_log_rows(legacy)[0]
        self.assertEqual(legacy_row["FR"], 1)
        self.assertEqual(legacy_row["pi_boot_id"], "")

        body = v2_payload()
        modern = {**legacy, "message": json.dumps(body)}
        modern_row = logger_app.build_log_rows(modern)[0]
        self.assertEqual(modern_row["pi_boot_id"], "boot-a")
        self.assertEqual(modern_row["batch_seq"], 10)
        self.assertEqual(modern_row["pi_mono_est_ns"], 1_100_000_010)

    def test_outbound_command_repeats_pending_until_report_and_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sync.sqlite3"
            store = CameraSyncStore(path)
            store.create_command(
                command_id="cmd-1",
                recording_id="rec-1",
                session_id="s1",
                device="camera",
                video_filename="camera-s1.mp4",
            )
            store.activate_command("cmd-1", "camera-s1.mp4")
            first = store.next_command("camera", "boot-a")
            second = store.next_command("camera", "boot-a")
            self.assertEqual(first["command_id"], "cmd-1")
            self.assertEqual(second["command_id"], "cmd-1")
            self.assertEqual(store.command("cmd-1")["status"], "pending")

            restarted = CameraSyncStore(path)
            restored = restarted.next_command("camera", "boot-a")
            self.assertEqual(restored["command_id"], "cmd-1")
            self.assertEqual(restarted.command("cmd-1")["status"], "pending")

    def test_report_is_idempotent_and_completes_only_after_storage(self) -> None:
        report = {
            "device": "camera",
            "command_id": "cmd-1",
            "recording_id": "rec-1",
            "session_id": "s1",
            "pi_boot_id": "boot-a",
            "first_frame_pi_mono_ns": 123,
            "first_frame_pts_ns": 0,
            "stream_first_sensor_mono_ns": 100,
            "camera_clock_source": "CLOCK_BOOTTIME",
            "rtp_timestamp": None,
            "rtp_clock_rate": 90000,
            "reported_pi_mono_ns": 200,
        }
        with tempfile.TemporaryDirectory() as temp:
            store = CameraSyncStore(Path(temp) / "sync.sqlite3")
            store.create_command(
                command_id="cmd-1",
                recording_id="rec-1",
                session_id="s1",
                device="camera",
                video_filename="camera-s1.mp4",
            )
            store.activate_command("cmd-1", "camera-s1.mp4")
            store.next_command("camera", "boot-a")
            first = store.save_report(report)
            self.assertFalse(first["duplicate"])
            self.assertEqual(store.command("cmd-1")["status"], "pending")
            store.complete_command("cmd-1")
            self.assertEqual(store.command("cmd-1")["status"], "completed")
            duplicate = store.save_report(report)
            self.assertTrue(duplicate["duplicate"])

    def test_device_api_bearer_get_and_duplicate_report(self) -> None:
        token = "test-device-token"
        report = {
            "device": "camera",
            "command_id": "cmd-1",
            "recording_id": "rec-1",
            "session_id": "s1",
            "pi_boot_id": "boot-a",
            "first_frame_pi_mono_ns": 123,
            "first_frame_pts_ns": 0,
            "stream_first_sensor_mono_ns": 100,
            "camera_clock_source": "CLOCK_BOOTTIME",
            "rtp_timestamp": None,
            "rtp_clock_rate": 90000,
            "reported_pi_mono_ns": 200,
        }
        with tempfile.TemporaryDirectory() as temp:
            store = CameraSyncStore(Path(temp) / "sync.sqlite3")
            store.create_command(
                command_id="cmd-1",
                recording_id="rec-1",
                session_id="s1",
                device="camera",
                video_filename="camera-s1.mp4",
            )
            store.activate_command("cmd-1", "camera-s1.mp4")
            sidecar = Path(temp) / "camera-s1.mp4.sync.json"
            with (
                patch.object(logger_app, "CAMERA_SYNC_SERVER_TOKEN", token),
                patch.object(logger_app, "camera_sync_store", store),
                patch.object(
                    logger_app,
                    "_write_camera_sync_sidecar",
                    return_value=sidecar,
                ),
                patch.object(logger_app, "_write_session_sync"),
            ):
                unauthorized = logger_app.camera_sync_next(
                    DummyRequest(),
                    "camera",
                    "boot-a",
                )
                self.assertEqual(unauthorized.status_code, 401)
                next_response = logger_app.camera_sync_next(
                    DummyRequest(token=token),
                    "camera",
                    "boot-a",
                )
                self.assertEqual(next_response.status_code, 200)
                self.assertEqual(
                    json.loads(next_response.body)["command_id"],
                    "cmd-1",
                )
                first = asyncio.run(
                    logger_app.camera_sync_report(
                        DummyRequest(report, token=token)
                    )
                )
                second = asyncio.run(
                    logger_app.camera_sync_report(
                        DummyRequest(report, token=token)
                    )
                )
            self.assertFalse(json.loads(first.body)["duplicate"])
            self.assertTrue(json.loads(second.body)["duplicate"])
            self.assertEqual(store.command("cmd-1")["status"], "completed")
            self.assertIsNone(store.next_command("camera", "boot-a"))

    def test_session_video_sidecar_contains_pi_data_and_explicit_rtcp_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "camera-s1.mp4.sync.json"
            with patch.object(
                logger_app,
                "_camera_sync_sidecar_path",
                return_value=target,
            ):
                path = logger_app._write_camera_sync_sidecar(
                    "s1",
                    "camera-s1.mp4",
                    {
                        "status": "ready",
                        "pi_boot_id": "boot-a",
                        "first_frame_pi_mono_ns": 123,
                        "first_frame_pts_ns": 0,
                        "camera_clock_source": "CLOCK_BOOTTIME",
                        "rtp_timestamp": None,
                        "rtp_clock_rate": 90000,
                        "stream_first_sensor_mono_ns": 100,
                    },
                )
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["session_id"], "s1")
            self.assertEqual(saved["first_frame_pi_mono_ns"], 123)
            self.assertFalse(saved["mapping_ready"])
            self.assertEqual(saved["rtcp_mapping"]["status"], "unavailable")
            self.assertIn("노출하지 않음", saved["rtcp_mapping"]["reason"])

    def test_common_pi_timeline_query_with_synthetic_rtp_anchors(self) -> None:
        session_sync = {"camera_filename": "camera-s1.mp4"}
        sidecar = {
            "rtp_timestamp": 1000,
            "first_frame_pi_mono_ns": 5_000_000_000,
            "rtp_clock_rate": 90000,
            "rtcp_mapping": {
                "recording_first_rtp_timestamp": 1000,
                "rtp_clock_rate": 90000,
            },
        }
        sensor = {"pi_mono_est_ns": "6000000000", "FR": "1.2"}
        with (
            patch.object(
                logger_app,
                "_read_json_object",
                side_effect=[session_sync, sidecar],
            ),
            patch.object(
                logger_app,
                "_nearest_sensor_row",
                return_value=(sensor, 0),
            ),
        ):
            result = logger_app.sync_timeline("s1", 1_000_000_000)
        self.assertTrue(result["mapping_ready"])
        self.assertEqual(result["video_frame_pi_mono_ns"], 6_000_000_000)
        self.assertEqual(result["sensor"]["FR"], "1.2")

    def test_rtp_wrap_and_video_pts_mapping(self) -> None:
        mapped = video_pts_to_pi_mono_ns(
            video_pts_ns=1_000_000_000,
            pi_rtp_anchor=0xFFFF_F000,
            pi_mono_anchor_ns=10_000_000_000,
            recording_first_rtp=0xFFFF_F000,
            rtp_clock_rate=90000,
        )
        self.assertEqual(mapped, 11_000_000_000)

        ntp_ns = rtp_to_ntp_ns(
            rtp_timestamp=(0xFFFF_F000 + 90000) % (1 << 32),
            sender_report_rtp_timestamp=0xFFFF_F000,
            sender_report_ntp_ns=20_000_000_000,
            rtp_clock_rate=90000,
        )
        self.assertEqual(ntp_ns, 21_000_000_000)

    def test_rtcp_sender_report_parser_and_sidecar_finalize(self) -> None:
        ntp_seconds = NTP_UNIX_EPOCH_DELTA + 100
        packet = struct.pack(
            "!BBHIIIIII",
            0x80,
            200,
            6,
            0x12345678,
            ntp_seconds,
            0,
            9000,
            1,
            100,
        )
        reports = parse_rtcp_sender_reports(packet)
        self.assertEqual(reports[0]["ssrc"], 0x12345678)
        self.assertEqual(reports[0]["ntp_unix_ns"], 100_000_000_000)

        with tempfile.TemporaryDirectory() as temp:
            sidecar_path = Path(temp) / "video.mp4.sync.json"
            sidecar_path.write_text(
                json.dumps(
                    {
                        "reported_pi_mono_ns": 5_000_000_000,
                        "reported_pi_realtime_ns": 100_000_000_000,
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(
                logger_app,
                "_camera_sync_sidecar_path",
                return_value=sidecar_path,
            ):
                result = logger_app._finalize_rtp_sync_sidecar(
                    "s1",
                    "video.mp4",
                    {
                        "mapping_source": "test",
                        "recorder": {
                            "ssrc": 0x12345678,
                            "first_rtp_timestamp": 9000,
                            "first_sequence": 1,
                            "rtp_clock_rate": 90000,
                        },
                        "rtcp_sr": reports[0],
                    },
                )
            saved = json.loads(sidecar_path.read_text(encoding="utf-8"))
            self.assertTrue(result["mapping_ready"])
            self.assertEqual(
                saved["rtcp_mapping"]["recording_first_rtp_pi_mono_ns"],
                5_000_000_000,
            )


if __name__ == "__main__":
    unittest.main()
