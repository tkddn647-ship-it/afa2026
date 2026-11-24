from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class PendingCommandExists(RuntimeError):
    pass


class SyncReportError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CameraSyncStore:
    """Durable outbound command queue and idempotent Pi sync reports."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS camera_sync_commands (
                    command_id TEXT PRIMARY KEY,
                    recording_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL UNIQUE,
                    device TEXT NOT NULL,
                    pi_boot_id TEXT NOT NULL DEFAULT '',
                    video_filename TEXT NOT NULL,
                    available INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'completed')),
                    created_at TEXT NOT NULL,
                    delivered_at TEXT NOT NULL DEFAULT '',
                    completed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_camera_sync_one_pending_device
                    ON camera_sync_commands(device)
                    WHERE status='pending';
                CREATE INDEX IF NOT EXISTS idx_camera_sync_pending
                    ON camera_sync_commands(device, status, created_at);

                CREATE TABLE IF NOT EXISTS camera_sync_reports (
                    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_id TEXT NOT NULL UNIQUE,
                    recording_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    device TEXT NOT NULL,
                    pi_boot_id TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(device, pi_boot_id, session_id),
                    FOREIGN KEY(command_id)
                        REFERENCES camera_sync_commands(command_id)
                );
                """
            )
            columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(camera_sync_commands)"
                ).fetchall()
            }
            if "available" not in columns:
                conn.execute(
                    "ALTER TABLE camera_sync_commands "
                    "ADD COLUMN available INTEGER NOT NULL DEFAULT 0"
                )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def create_command(
        self,
        *,
        command_id: str,
        recording_id: str,
        session_id: str,
        device: str,
        video_filename: str,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO camera_sync_commands(
                        command_id, recording_id, session_id, device,
                        video_filename, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        command_id,
                        recording_id,
                        session_id,
                        device,
                        video_filename,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                pending = conn.execute(
                    """
                    SELECT * FROM camera_sync_commands
                    WHERE device=? AND status='pending'
                    """,
                    (device,),
                ).fetchone()
                if pending is not None:
                    raise PendingCommandExists(
                        f"{device}에 완료되지 않은 sync command가 있습니다."
                    ) from exc
                raise
            row = conn.execute(
                "SELECT * FROM camera_sync_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
            return dict(row)

    def next_command(self, device: str, pi_boot_id: str) -> dict[str, Any] | None:
        now = _utc_now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM camera_sync_commands
                WHERE device=? AND status='pending'
                  AND available=1
                  AND (pi_boot_id='' OR pi_boot_id=?)
                ORDER BY created_at
                LIMIT 1
                """,
                (device, pi_boot_id),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            command_id = str(row["command_id"])
            conn.execute(
                """
                UPDATE camera_sync_commands
                SET pi_boot_id=CASE
                        WHEN pi_boot_id='' THEN ?
                        ELSE pi_boot_id
                    END,
                    delivered_at=CASE
                        WHEN delivered_at='' THEN ?
                        ELSE delivered_at
                    END
                WHERE command_id=?
                """,
                (pi_boot_id, now, command_id),
            )
            updated = conn.execute(
                "SELECT * FROM camera_sync_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
            conn.commit()
            return self._row(updated)

    def save_report(self, report: dict[str, Any]) -> dict[str, Any]:
        command_id = str(report["command_id"])
        recording_id = str(report["recording_id"])
        session_id = str(report["session_id"])
        device = str(report["device"])
        pi_boot_id = str(report["pi_boot_id"])
        now = _utc_now()
        encoded = json.dumps(report, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            command = conn.execute(
                "SELECT * FROM camera_sync_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
            if command is None:
                raise SyncReportError(f"unknown command_id={command_id}")
            if str(command["recording_id"]) != recording_id:
                raise SyncReportError("recording_id mismatch")
            if str(command["session_id"]) != session_id:
                raise SyncReportError("session_id mismatch")
            if str(command["device"]) != device:
                raise SyncReportError("device mismatch")

            existing = conn.execute(
                "SELECT * FROM camera_sync_reports WHERE command_id=?",
                (command_id,),
            ).fetchone()
            if existing is not None:
                return {
                    **dict(command),
                    "pi_boot_id": str(existing["pi_boot_id"]),
                    "received_at": str(existing["received_at"]),
                    "duplicate": True,
                    "payload": json.loads(existing["payload_json"]),
                }

            conn.execute(
                """
                INSERT INTO camera_sync_reports(
                    command_id, recording_id, session_id, device,
                    pi_boot_id, received_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command_id,
                    recording_id,
                    session_id,
                    device,
                    pi_boot_id,
                    now,
                    encoded,
                ),
            )
            conn.commit()
            return {
                **dict(command),
                "pi_boot_id": pi_boot_id,
                "received_at": now,
                "duplicate": False,
                "payload": report,
            }

    def complete_command(self, command_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            report = conn.execute(
                "SELECT 1 FROM camera_sync_reports WHERE command_id=?",
                (command_id,),
            ).fetchone()
            if report is None:
                conn.rollback()
                raise SyncReportError("report 저장 전에는 command를 완료할 수 없습니다.")
            conn.execute(
                """
                UPDATE camera_sync_commands
                SET status='completed', completed_at=?
                WHERE command_id=?
                """,
                (_utc_now(), command_id),
            )
            conn.commit()

    def activate_command(self, command_id: str, video_filename: str) -> None:
        safe_name = Path(video_filename).name
        if not safe_name:
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE camera_sync_commands
                SET video_filename=?, available=1
                WHERE command_id=? AND status='pending'
                """,
                (safe_name, command_id),
            )

    def abandon_pending(
        self,
        *,
        device: str | None = None,
        command_id: str | None = None,
    ) -> int:
        """Pi report 없이 남은 pending sync를 버려 로깅 재시작이 막히지 않게 한다."""
        now = _utc_now()
        with self._lock, self._connect() as conn:
            if command_id:
                cur = conn.execute(
                    """
                    UPDATE camera_sync_commands
                    SET status='completed', completed_at=?, available=0
                    WHERE command_id=? AND status='pending'
                    """,
                    (now, command_id),
                )
            elif device:
                cur = conn.execute(
                    """
                    UPDATE camera_sync_commands
                    SET status='completed', completed_at=?, available=0
                    WHERE device=? AND status='pending'
                    """,
                    (now, device),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE camera_sync_commands
                    SET status='completed', completed_at=?, available=0
                    WHERE status='pending'
                    """,
                    (now,),
                )
            return int(cur.rowcount or 0)

    def command(self, command_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM camera_sync_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
            return self._row(row)
