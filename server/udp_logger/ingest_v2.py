from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


V2_REQUIRED_BATCH_FIELDS = (
    "pi_boot_id",
    "batch_seq",
    "uart_rx_mono_ns",
    "stm_epoch",
)


class V2ValidationError(ValueError):
    pass


def _as_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise V2ValidationError(f"{field}는 정수여야 합니다.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise V2ValidationError(f"{field}는 정수여야 합니다.") from exc


def validate_v2_payload(body: dict[str, Any]) -> dict[str, Any]:
    if _as_int(body.get("schema_version"), "schema_version") != 2:
        raise V2ValidationError("schema_version=2 payload가 아닙니다.")

    for field in V2_REQUIRED_BATCH_FIELDS:
        if body.get(field) in (None, ""):
            raise V2ValidationError(f"{field}가 필요합니다.")

    device = str(body.get("device") or "").strip()
    boot_id = str(body.get("pi_boot_id") or "").strip()
    if not device:
        raise V2ValidationError("device가 필요합니다.")
    if not boot_id:
        raise V2ValidationError("pi_boot_id가 필요합니다.")

    samples = body.get("samples")
    if not isinstance(samples, list) or not samples:
        raise V2ValidationError("samples는 비어 있지 않은 배열이어야 합니다.")
    if not all(isinstance(sample, dict) for sample in samples):
        raise V2ValidationError("samples의 모든 항목은 객체여야 합니다.")

    sample_count = _as_int(body.get("sample_count"), "sample_count")
    if sample_count != len(samples):
        raise V2ValidationError(
            f"sample_count({sample_count}) != len(samples)({len(samples)})"
        )

    last_t = samples[-1].get("t")
    last_sample_stm_ms = _as_int(body.get("last_sample_stm_ms"), "last_sample_stm_ms")
    if last_t is None or last_sample_stm_ms != _as_int(last_t, "samples[-1].t"):
        raise V2ValidationError(
            "last_sample_stm_ms가 samples[-1].t와 일치하지 않습니다."
        )

    return {
        "device": device,
        "pi_boot_id": boot_id,
        "sender_instance_id": str(body.get("sender_instance_id") or ""),
        "batch_seq": _as_int(body.get("batch_seq"), "batch_seq"),
        "uart_rx_mono_ns": _as_int(body.get("uart_rx_mono_ns"), "uart_rx_mono_ns"),
        "first_sample_stm_ms": _as_int(
            body.get("first_sample_stm_ms"), "first_sample_stm_ms"
        ),
        "last_sample_stm_ms": last_sample_stm_ms,
        "sample_count": sample_count,
        "stm_epoch": _as_int(body.get("stm_epoch"), "stm_epoch"),
        "stm_wrap_count": _as_int(body.get("stm_wrap_count", 0), "stm_wrap_count"),
    }


def batch_metadata(body: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in body.items()
        if key != "samples"
    }


class IngestV2Store:
    """SQLite-backed durable deduplication and sequence/clock metadata."""

    def __init__(self, db_path: Path, *, retention_days: int = 30) -> None:
        self.db_path = db_path
        self.retention_days = max(1, int(retention_days))
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ingest_batches (
                    device TEXT NOT NULL,
                    pi_boot_id TEXT NOT NULL,
                    batch_seq INTEGER NOT NULL,
                    sender_instance_id TEXT NOT NULL DEFAULT '',
                    received_at TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    uart_rx_mono_ns INTEGER NOT NULL,
                    first_sample_stm_ms INTEGER NOT NULL,
                    last_sample_stm_ms INTEGER NOT NULL,
                    sample_count INTEGER NOT NULL,
                    stm_epoch INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    PRIMARY KEY (device, pi_boot_id, batch_seq)
                );
                CREATE INDEX IF NOT EXISTS idx_ingest_batches_received
                    ON ingest_batches(received_at);
                CREATE INDEX IF NOT EXISTS idx_ingest_batches_session
                    ON ingest_batches(session_id);

                CREATE TABLE IF NOT EXISTS stm_clock_models (
                    device TEXT NOT NULL,
                    pi_boot_id TEXT NOT NULL,
                    stm_epoch INTEGER NOT NULL,
                    last_batch_seq INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    clock_anchor_json TEXT NOT NULL,
                    clock_model_json TEXT NOT NULL,
                    PRIMARY KEY (device, pi_boot_id, stm_epoch)
                );
                """
            )
        self.prune()

    def register_batch(
        self,
        body: dict[str, Any],
        validated: dict[str, Any],
        *,
        session_id: str = "",
        received_at: str | None = None,
    ) -> dict[str, Any]:
        received = received_at or datetime.now(timezone.utc).isoformat()
        metadata = batch_metadata(body)
        with self._lock, self._connect() as conn:
            previous_max_row = conn.execute(
                """
                SELECT MAX(batch_seq) AS max_seq
                FROM ingest_batches
                WHERE device=? AND pi_boot_id=?
                """,
                (validated["device"], validated["pi_boot_id"]),
            ).fetchone()
            previous_max = (
                int(previous_max_row["max_seq"])
                if previous_max_row and previous_max_row["max_seq"] is not None
                else None
            )
            try:
                conn.execute(
                    """
                    INSERT INTO ingest_batches(
                        device, pi_boot_id, batch_seq, sender_instance_id,
                        received_at, session_id, uart_rx_mono_ns,
                        first_sample_stm_ms, last_sample_stm_ms, sample_count,
                        stm_epoch, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        validated["device"],
                        validated["pi_boot_id"],
                        validated["batch_seq"],
                        validated["sender_instance_id"],
                        received,
                        session_id,
                        validated["uart_rx_mono_ns"],
                        validated["first_sample_stm_ms"],
                        validated["last_sample_stm_ms"],
                        validated["sample_count"],
                        validated["stm_epoch"],
                        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
            except sqlite3.IntegrityError:
                return {
                    "accepted": False,
                    "duplicate": True,
                    "out_of_order": False,
                }

            conn.execute(
                """
                INSERT INTO stm_clock_models(
                    device, pi_boot_id, stm_epoch, last_batch_seq, updated_at,
                    clock_anchor_json, clock_model_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device, pi_boot_id, stm_epoch) DO UPDATE SET
                    last_batch_seq=excluded.last_batch_seq,
                    updated_at=excluded.updated_at,
                    clock_anchor_json=excluded.clock_anchor_json,
                    clock_model_json=excluded.clock_model_json
                WHERE excluded.last_batch_seq >= stm_clock_models.last_batch_seq
                """,
                (
                    validated["device"],
                    validated["pi_boot_id"],
                    validated["stm_epoch"],
                    validated["batch_seq"],
                    received,
                    json.dumps(body.get("stm_clock_anchor"), ensure_ascii=False),
                    json.dumps(body.get("stm_clock_model"), ensure_ascii=False),
                ),
            )
            return {
                "accepted": True,
                "duplicate": False,
                "out_of_order": (
                    previous_max is not None
                    and validated["batch_seq"] < previous_max
                ),
            }

    def sequence_status(self, device: str, pi_boot_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT batch_seq FROM ingest_batches
                WHERE device=? AND pi_boot_id=?
                ORDER BY batch_seq
                """,
                (device, pi_boot_id),
            ).fetchall()
        sequences = [int(row["batch_seq"]) for row in rows]
        missing_ranges: list[dict[str, int]] = []
        for previous, current in zip(sequences, sequences[1:]):
            if current > previous + 1:
                missing_ranges.append(
                    {
                        "start": previous + 1,
                        "end": current - 1,
                        "count": current - previous - 1,
                    }
                )
        return {
            "device": device,
            "pi_boot_id": pi_boot_id,
            "received_count": len(sequences),
            "min_seq": sequences[0] if sequences else None,
            "max_seq": sequences[-1] if sequences else None,
            "missing_count": sum(item["count"] for item in missing_ranges),
            "missing_ranges": missing_ranges,
            "retention_days": self.retention_days,
        }

    def ordered_batch_metadata(
        self, device: str, pi_boot_id: str
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT metadata_json FROM ingest_batches
                WHERE device=? AND pi_boot_id=?
                ORDER BY batch_seq
                """,
                (device, pi_boot_id),
            ).fetchall()
        return [json.loads(str(row["metadata_json"])) for row in rows]

    def prune(self) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.retention_days)).isoformat()
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM ingest_batches WHERE received_at < ?",
                (cutoff,),
            )
            return max(0, int(cursor.rowcount or 0))
