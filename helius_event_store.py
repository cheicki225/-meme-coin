"""Crash-safe inbox for events received from Helius WebSocket subscriptions.

Signatures remain in this local SQLite database for durable deduplication.
The pending inbox is bounded by both row count and serialized payload bytes.
When either bound is reached, intake is paused and history replay is required.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from execution_metrics import (
    correlate_event,
    measure_execution,
    record_execution_metric,
)


log = logging.getLogger("helius_event_store")

_SCHEMA_VERSION = 4
_PAGE_LIMIT = 1000
_ACTIVE_STATES = ("pending", "retry", "processing", "unknown")


class EventStoreError(RuntimeError):
    """Raised when the durable event inbox cannot safely read or write state."""


class EventQueueFullError(EventStoreError):
    """Raised when a notification cannot be durably added within configured bounds."""


class EventIntakePausedError(EventStoreError):
    """Raised when new live events arrive before a stream's history is recovered."""


class EventRecoveryError(EventStoreError):
    """Raised when history replay cannot prove the gap was completely recovered."""


class CanonicalOrderRequiredError(EventStoreError):
    """Raised when an event cannot be assigned a provider-canonical order."""


class RetryableEventError(RuntimeError):
    """Raised when an event is durably queued but its read-only parse must be retried."""

    def __init__(self, code: str = "enhanced_fetch_unavailable") -> None:
        self.code = HeliusEventStore._safe_error_code(code)
        super().__init__(self.code)


@dataclass(frozen=True)
class EventRecord:
    """One claimed inbox event; its signature is stored locally, never in telemetry."""

    event_id: int
    stream: str
    sequence: int
    signature: str
    correlation_id: str
    slot: int | None
    payload: dict
    attempts: int
    received_at: float
    transaction_index: int | None = None


@dataclass(frozen=True)
class EnqueueResult:
    event_id: int
    inserted: bool
    status: str
    correlation_id: str


class HeliusEventStore:
    """Persist, deduplicate, retry, and checkpoint Helius events using SQLite WAL."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_pending_events: int = 10_000,
        max_pending_bytes: int = 32 * 1024 * 1024,
        lease_seconds: float = 120.0,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 60.0,
        timeout_seconds: float = 10.0,
    ) -> None:
        if type(max_pending_events) is not int or max_pending_events < 1:
            raise ValueError("max_pending_events must be a positive integer")
        if type(max_pending_bytes) is not int or max_pending_bytes < 1:
            raise ValueError("max_pending_bytes must be a positive integer")
        if lease_seconds <= 0 or retry_base_seconds <= 0 or retry_max_seconds <= 0:
            raise ValueError("lease and retry intervals must be positive")
        self.path = Path(path)
        self.max_pending_events = max_pending_events
        self.max_pending_bytes = max_pending_bytes
        self.lease_seconds = lease_seconds
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.timeout_seconds = timeout_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=self.timeout_seconds)
        db.execute(f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}")
        db.execute("PRAGMA synchronous = FULL")
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _initialize(self) -> None:
        try:
            with self._connect() as db:
                mode = db.execute("PRAGMA journal_mode = WAL").fetchone()
                if not mode or str(mode[0]).lower() != "wal":
                    raise EventStoreError("Helius event inbox could not enable WAL mode")
                integrity = db.execute("PRAGMA integrity_check").fetchall()
                if integrity != [("ok",)]:
                    raise EventStoreError("Helius event inbox integrity check failed")
                version = int(db.execute("PRAGMA user_version").fetchone()[0])
                if version > _SCHEMA_VERSION:
                    raise EventStoreError("Helius event inbox schema is newer than this bot")
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS event_streams (
                        stream TEXT PRIMARY KEY,
                        next_sequence INTEGER NOT NULL DEFAULT 0,
                        checkpoint_sequence INTEGER NOT NULL DEFAULT 0,
                        checkpoint_signature TEXT,
                        recovery_required INTEGER NOT NULL DEFAULT 0,
                        intake_paused INTEGER NOT NULL DEFAULT 0,
                        blocked_reason TEXT
                    );
                    CREATE TABLE IF NOT EXISTS event_inbox (
                        event_id INTEGER PRIMARY KEY,
                        stream TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        signature TEXT NOT NULL,
                        correlation_id TEXT NOT NULL DEFAULT '',
                        slot INTEGER,
                        transaction_index INTEGER,
                        payload_json TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN ('pending','retry','processing','processed','unknown')
                        ),
                        attempts INTEGER NOT NULL DEFAULT 0,
                        effect_started INTEGER NOT NULL DEFAULT 0,
                        next_retry_at REAL NOT NULL DEFAULT 0,
                        lease_until REAL NOT NULL DEFAULT 0,
                        received_at REAL NOT NULL,
                        processed_at REAL,
                        last_error_code TEXT,
                        UNIQUE(stream, sequence),
                        UNIQUE(stream, signature),
                        FOREIGN KEY(stream) REFERENCES event_streams(stream)
                    );
                    CREATE INDEX IF NOT EXISTS idx_event_inbox_pending
                        ON event_inbox(stream, sequence, status, next_retry_at);
                    CREATE TABLE IF NOT EXISTS event_counters (
                        name TEXT PRIMARY KEY,
                        value INTEGER NOT NULL
                    );
                    """
                )
                columns = {
                    str(row[1]) for row in db.execute("PRAGMA table_info(event_inbox)")
                }
                if "correlation_id" not in columns:
                    db.execute(
                        "ALTER TABLE event_inbox ADD COLUMN "
                        "correlation_id TEXT NOT NULL DEFAULT ''"
                    )
                if "effect_started" not in columns:
                    db.execute(
                        "ALTER TABLE event_inbox ADD COLUMN "
                        "effect_started INTEGER NOT NULL DEFAULT 0"
                    )
                if "transaction_index" not in columns:
                    db.execute("ALTER TABLE event_inbox ADD COLUMN transaction_index INTEGER")
                if version < _SCHEMA_VERSION:
                    db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        except sqlite3.DatabaseError as exc:
            raise EventStoreError("Helius event inbox could not be initialized safely") from exc

    @staticmethod
    def _validate_stream(stream: str) -> str:
        if not isinstance(stream, str) or not stream.strip() or len(stream) > 200:
            raise ValueError("stream must be a non-empty short identifier")
        return stream

    @staticmethod
    def _validate_signature(signature: str) -> str:
        if not isinstance(signature, str) or not signature or len(signature) > 128:
            raise ValueError("signature must be a non-empty bounded string")
        return signature

    @staticmethod
    def _counter(db: sqlite3.Connection, name: str, amount: int = 1) -> None:
        db.execute(
            """
            INSERT INTO event_counters(name, value) VALUES(?, ?)
            ON CONFLICT(name) DO UPDATE SET value = value + excluded.value
            """,
            (name, amount),
        )

    def enqueue(
        self,
        stream: str,
        signature: str,
        payload: dict | None = None,
        *,
        slot: int | None = None,
        transaction_index: int | None = None,
        is_replay: bool = False,
        now: float | None = None,
    ) -> EnqueueResult:
        """Durably insert before parsing; duplicates reuse the existing row."""
        stream = self._validate_stream(stream)
        signature = self._validate_signature(signature)
        if slot is not None and (type(slot) is not int or slot < 0):
            raise ValueError("slot must be a non-negative integer")
        if transaction_index is not None and (
            type(transaction_index) is not int or transaction_index < 0
        ):
            raise ValueError("transaction_index must be a non-negative integer")
        if is_replay and (slot is None or transaction_index is None):
            raise EventRecoveryError(
                "History replay requires canonical slot and transaction index"
            )
        if not is_replay and transaction_index is not None:
            raise ValueError("canonical transaction index is accepted only from history replay")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
        encoded_bytes = len(encoded.encode("utf-8"))
        payload_too_large = encoded_bytes > self.max_pending_bytes
        received_at = time.time() if now is None else float(now)
        correlation_id = uuid.uuid4().hex
        started = time.perf_counter()
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT OR IGNORE INTO event_streams(stream) VALUES(?)", (stream,)
                )
                existing = db.execute(
                    "SELECT event_id, status, correlation_id FROM event_inbox "
                    "WHERE stream=? AND signature=?",
                    (stream, signature),
                ).fetchone()
                if existing:
                    self._counter(db, "duplicates")
                    if is_replay:
                        self._counter(db, "replayed")
                    db.commit()
                    correlation_id = str(existing[2] or correlation_id)
                    result = EnqueueResult(
                        int(existing[0]), False, str(existing[1]), correlation_id
                    )
                    with correlate_event(correlation_id):
                        record_execution_metric(
                            "event.inbox_enqueue",
                            (time.perf_counter() - started) * 1000,
                            "skipped",
                            "helius",
                        )
                    return result

                stream_state = db.execute(
                    "SELECT recovery_required, intake_paused FROM event_streams "
                    "WHERE stream=?",
                    (stream,),
                ).fetchone()
                if (
                    not is_replay
                    and stream_state
                    and (bool(stream_state[0]) or bool(stream_state[1]))
                ):
                    self._counter(db, "paused_intake_rejections")
                    db.commit()
                    with correlate_event(correlation_id):
                        record_execution_metric(
                            "event.inbox_enqueue",
                            (time.perf_counter() - started) * 1000,
                            "failure",
                            "helius",
                        )
                    raise EventIntakePausedError(
                        "Helius event intake is paused until history recovery completes"
                    )

                pending_count, pending_bytes = db.execute(
                    """
                    SELECT COUNT(*), COALESCE(SUM(length(payload_json)), 0)
                    FROM event_inbox WHERE status != 'processed'
                    """
                ).fetchone()
                if (
                    payload_too_large
                    or
                    int(pending_count) >= self.max_pending_events
                    or int(pending_bytes) + encoded_bytes > self.max_pending_bytes
                ):
                    db.execute(
                        """
                        UPDATE event_streams SET recovery_required=1, intake_paused=1,
                            blocked_reason='queue_full' WHERE stream=?
                        """,
                        (stream,),
                    )
                    self._counter(db, "queue_full")
                    db.commit()
                    with correlate_event(correlation_id):
                        record_execution_metric(
                            "event.inbox_enqueue",
                            (time.perf_counter() - started) * 1000,
                            "failure",
                            "helius",
                        )
                    raise EventQueueFullError("Helius event inbox is full; stream intake paused")

                row = db.execute(
                    "SELECT next_sequence FROM event_streams WHERE stream=?", (stream,)
                ).fetchone()
                sequence = int(row[0]) + 1
                cursor = db.execute(
                    """
                    INSERT INTO event_inbox(
                        stream, sequence, signature, correlation_id, slot,
                        transaction_index, payload_json, status, received_at
                    ) VALUES(?,?,?,?,?,?,?,'pending',?)
                    """,
                    (
                        stream, sequence, signature, correlation_id, slot,
                        transaction_index, encoded,
                        received_at,
                    ),
                )
                if cursor.lastrowid is None:
                    raise EventStoreError("Helius event insert returned no event ID")
                event_id = cursor.lastrowid
                db.execute(
                    "UPDATE event_streams SET next_sequence=? WHERE stream=?",
                    (sequence, stream),
                )
                self._counter(db, "received")
                if is_replay:
                    self._counter(db, "replayed")
                db.commit()
                result = EnqueueResult(
                    event_id, True, "pending", correlation_id
                )
                with correlate_event(correlation_id):
                    record_execution_metric(
                        "event.inbox_enqueue",
                        (time.perf_counter() - started) * 1000,
                        "success",
                        "helius",
                    )
                return result
        except sqlite3.DatabaseError as exc:
            raise EventStoreError("Helius event could not be persisted") from exc

    def claim_next(self, stream: str, *, now: float | None = None) -> EventRecord | None:
        """Claim only the oldest unprocessed event in a stream.

        An UNKNOWN outcome or an active claim blocks later events. An expired
        claim is replayed only if no downstream effect was durably marked as
        started; otherwise it becomes UNKNOWN to avoid blindly duplicating an
        effect whose outcome cannot be proven.
        """
        stream = self._validate_stream(stream)
        current = time.time() if now is None else float(now)
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    """
                    SELECT event_id, sequence, signature, correlation_id, slot,
                           transaction_index,
                           payload_json, status,
                            attempts, next_retry_at, lease_until, received_at,
                            effect_started
                    FROM event_inbox
                    WHERE stream=? AND status != 'processed'
                    ORDER BY sequence LIMIT 1
                    """,
                    (stream,),
                ).fetchone()
                if not row:
                    return None
                (
                    event_id, sequence, signature, correlation_id, slot,
                    transaction_index,
                    raw_payload, status, attempts, retry_at, lease_until, received_at,
                    effect_started,
                ) = row
                if status == "unknown":
                    return None
                if status == "processing" and float(lease_until) > current:
                    return None
                if status == "processing" and bool(effect_started):
                    db.execute(
                        "UPDATE event_inbox SET status='unknown', lease_until=0, "
                        "last_error_code='lease_expired_after_effect' WHERE event_id=?",
                        (event_id,),
                    )
                    db.execute(
                        "UPDATE event_streams SET intake_paused=1, "
                        "blocked_reason='lease_expired_after_effect' WHERE stream=?",
                        (stream,),
                    )
                    self._counter(db, "unknown")
                    db.commit()
                    return None
                if status == "retry" and float(retry_at) > current:
                    return None
                try:
                    payload = json.loads(raw_payload)
                except (json.JSONDecodeError, TypeError) as exc:
                    db.execute(
                        "UPDATE event_inbox SET status='unknown', last_error_code='invalid_payload' WHERE event_id=?",
                        (event_id,),
                    )
                    db.commit()
                    raise EventStoreError("Helius inbox payload is corrupt; stream is blocked") from exc
                if not isinstance(payload, dict):
                    db.execute(
                        "UPDATE event_inbox SET status='unknown', last_error_code='invalid_payload' WHERE event_id=?",
                        (event_id,),
                    )
                    db.commit()
                    raise EventStoreError("Helius inbox payload is corrupt; stream is blocked")
                if not correlation_id:
                    correlation_id = uuid.uuid4().hex
                    db.execute(
                        "UPDATE event_inbox SET correlation_id=? WHERE event_id=?",
                        (correlation_id, event_id),
                    )
                db.execute(
                    """
                    UPDATE event_inbox SET status='processing', attempts=attempts+1,
                        lease_until=?, next_retry_at=0, effect_started=0 WHERE event_id=?
                    """,
                    (current + self.lease_seconds, event_id),
                )
                self._counter(db, "claimed")
                return EventRecord(
                    int(event_id), stream, int(sequence), str(signature),
                    str(correlation_id or uuid.uuid4().hex), slot,
                    payload, int(attempts) + 1, float(received_at), transaction_index,
                )
        except sqlite3.DatabaseError as exc:
            raise EventStoreError("Helius inbox claim failed") from exc

    def mark_retry(
        self,
        event_id: int,
        error_code: str,
        *,
        now: float | None = None,
        delay_seconds: float | None = None,
    ) -> float:
        """Return a failed parse/fetch to the queue with bounded exponential delay."""
        current = time.time() if now is None else float(now)
        code = self._safe_error_code(error_code)
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT attempts, effect_started FROM event_inbox "
                    "WHERE event_id=? AND status='processing'",
                    (event_id,),
                ).fetchone()
                if not row:
                    raise EventStoreError("Helius event is not claimed for retry")
                if bool(row[1]):
                    raise EventStoreError(
                        "Helius event cannot be retried after downstream effects started"
                    )
                attempts = max(1, int(row[0]))
                delay = (
                    min(self.retry_base_seconds * (2 ** min(attempts - 1, 20)), self.retry_max_seconds)
                    if delay_seconds is None
                    else min(max(float(delay_seconds), 0.0), self.retry_max_seconds)
                )
                db.execute(
                    """
                    UPDATE event_inbox SET status='retry', next_retry_at=?, lease_until=0,
                        last_error_code=? WHERE event_id=?
                    """,
                    (current + delay, code, event_id),
                )
                self._counter(db, "retried")
                return delay
        except sqlite3.DatabaseError as exc:
            raise EventStoreError("Helius event retry state could not be persisted") from exc

    def mark_effect_started(self, event_id: int) -> None:
        """Persist the boundary before invoking a callback with possible side effects."""
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                cursor = db.execute(
                    "UPDATE event_inbox SET effect_started=1 WHERE event_id=? "
                    "AND status='processing'",
                    (event_id,),
                )
                if cursor.rowcount != 1:
                    raise EventStoreError("Helius event is not claimed before effect")
        except sqlite3.DatabaseError as exc:
            raise EventStoreError("Helius effect boundary could not be persisted") from exc

    def mark_unknown(self, event_id: int, error_code: str = "handler_outcome_unknown") -> None:
        """Block the stream when downstream effects may have partially occurred."""
        code = self._safe_error_code(error_code)
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT stream FROM event_inbox WHERE event_id=? AND status='processing'",
                    (event_id,),
                ).fetchone()
                if not row:
                    raise EventStoreError("Helius event is not claimed as processing")
                db.execute(
                    """
                    UPDATE event_inbox SET status='unknown', lease_until=0,
                        last_error_code=? WHERE event_id=?
                    """,
                    (code, event_id),
                )
                db.execute(
                    """
                    UPDATE event_streams SET intake_paused=1, blocked_reason=?
                    WHERE stream=?
                    """,
                    (code, row[0]),
                )
                self._counter(db, "unknown")
        except sqlite3.DatabaseError as exc:
            raise EventStoreError("Helius UNKNOWN outcome could not be persisted") from exc

    def mark_processed(self, event_id: int, *, now: float | None = None) -> None:
        """Acknowledge only after the event handler has returned successfully."""
        processed_at = time.time() if now is None else float(now)
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT stream, sequence, signature FROM event_inbox "
                    "WHERE event_id=? AND status='processing'",
                    (event_id,),
                ).fetchone()
                if not row:
                    raise EventStoreError("Helius event is not claimed for completion")
                stream = str(row[0])
                db.execute(
                    """
                    UPDATE event_inbox SET status='processed', payload_json='',
                        lease_until=0, processed_at=?, last_error_code=NULL
                    WHERE event_id=?
                    """,
                    (processed_at, event_id),
                )
                checkpoint = db.execute(
                    "SELECT checkpoint_sequence FROM event_streams WHERE stream=?", (stream,)
                ).fetchone()
                checkpoint_sequence = int(checkpoint[0]) if checkpoint else 0
                checkpoint_signature = None
                current = db.execute(
                    "SELECT checkpoint_signature FROM event_streams WHERE stream=?", (stream,)
                ).fetchone()
                if current:
                    checkpoint_signature = current[0]
                while True:
                    next_row = db.execute(
                        "SELECT sequence, signature, status FROM event_inbox "
                        "WHERE stream=? AND sequence=?",
                        (stream, checkpoint_sequence + 1),
                    ).fetchone()
                    if not next_row or next_row[2] != "processed":
                        break
                    checkpoint_sequence = int(next_row[0])
                    checkpoint_signature = str(next_row[1])
                db.execute(
                    """
                    UPDATE event_streams SET checkpoint_sequence=?, checkpoint_signature=?
                    WHERE stream=?
                    """,
                    (checkpoint_sequence, checkpoint_signature, stream),
                )
                self._counter(db, "processed")
        except sqlite3.DatabaseError as exc:
            raise EventStoreError("Helius event acknowledgement failed") from exc

    def checkpoint(self, stream: str) -> tuple[int, str | None]:
        stream = self._validate_stream(stream)
        with self._connect() as db:
            row = db.execute(
                "SELECT checkpoint_sequence, checkpoint_signature FROM event_streams WHERE stream=?",
                (stream,),
            ).fetchone()
        return (int(row[0]), row[1]) if row else (0, None)

    def pending_count(self, stream: str | None = None) -> int:
        where, args = ("status != 'processed'", ()) if stream is None else (
            "status != 'processed' AND stream=?", (self._validate_stream(stream),)
        )
        with self._connect() as db:
            return int(db.execute(
                f"SELECT COUNT(*) FROM event_inbox WHERE {where}", args
            ).fetchone()[0])

    def has_unknown(self, stream: str) -> bool:
        """Return whether an unresolved effect blocks this ordered stream."""
        stream = self._validate_stream(stream)
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM event_inbox WHERE stream=? AND status='unknown' LIMIT 1",
                (stream,),
            ).fetchone()
        return row is not None

    def stream_names(self, prefix: str) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT stream FROM event_streams WHERE stream LIKE ? ORDER BY stream",
                (f"{prefix}%",),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def ensure_stream(self, stream: str) -> None:
        """Persist a stream baseline without claiming it needs recovery yet."""
        stream = self._validate_stream(stream)
        try:
            with self._connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO event_streams(stream) VALUES(?)", (stream,)
                )
        except sqlite3.DatabaseError as exc:
            raise EventStoreError("Helius event stream baseline could not be saved") from exc

    def mark_recovery_required(self, stream: str, reason: str = "connection_gap") -> None:
        stream = self._validate_stream(stream)
        code = self._safe_error_code(reason)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                INSERT INTO event_streams(stream, recovery_required, intake_paused, blocked_reason)
                VALUES(?,1,1,?)
                ON CONFLICT(stream) DO UPDATE SET
                    recovery_required=1, intake_paused=1, blocked_reason=excluded.blocked_reason
                """,
                (stream, code),
            )

    def recovery_required_streams(self, prefix: str) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT stream FROM event_streams "
                "WHERE stream LIKE ? AND recovery_required=1 ORDER BY stream",
                (f"{prefix}%",),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def clear_intake_pause(self, stream: str) -> None:
        stream = self._validate_stream(stream)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM event_inbox WHERE stream=? AND status != 'processed' "
                "ORDER BY sequence LIMIT 1",
                (stream,),
            ).fetchone()
            if row and row[0] == "unknown":
                raise EventStoreError("UNKNOWN event blocks stream intake")
            db.execute(
                "UPDATE event_streams SET intake_paused=0 WHERE stream=?", (stream,)
            )

    def complete_recovery_if_idle(self, stream: str) -> bool:
        stream = self._validate_stream(stream)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute(
                "SELECT 1 FROM event_inbox WHERE stream=? AND status != 'processed' LIMIT 1",
                (stream,),
            ).fetchone()
            if pending:
                return False
            db.execute(
                """
                UPDATE event_streams SET recovery_required=0, intake_paused=0,
                    blocked_reason=NULL WHERE stream=?
                """,
                (stream,),
            )
            return True

    def intake_paused(self, stream: str) -> bool:
        stream = self._validate_stream(stream)
        with self._connect() as db:
            row = db.execute(
                "SELECT intake_paused FROM event_streams WHERE stream=?", (stream,)
            ).fetchone()
        return bool(row and row[0])

    def metrics(self) -> dict[str, int | float]:
        """Expose aggregate counts only; never return signatures or stream names."""
        with self._connect() as db:
            counts = dict(db.execute(
                "SELECT status, COUNT(*) FROM event_inbox GROUP BY status"
            ).fetchall())
            counters = dict(db.execute("SELECT name, value FROM event_counters").fetchall())
            pending_bytes = db.execute(
                "SELECT COALESCE(SUM(length(payload_json)),0) FROM event_inbox "
                "WHERE status != 'processed'"
            ).fetchone()[0]
        received = int(counters.get("received", 0))
        duplicates = int(counters.get("duplicates", 0))
        replayed = int(counters.get("replayed", 0))
        queue_full = int(counters.get("queue_full", 0))
        return {
            "pending": sum(int(counts.get(state, 0)) for state in _ACTIVE_STATES),
            "pending_bytes": int(pending_bytes),
            "processed": int(counts.get("processed", 0)),
            "unknown": int(counts.get("unknown", 0)),
            "received": received,
            "duplicates": duplicates,
            "replayed": replayed,
            "retried": int(counters.get("retried", 0)),
            "queue_full": queue_full,
            "paused_intake_rejections": int(
                counters.get("paused_intake_rejections", 0)
            ),
            "unpersisted_rejection_rate": round(
                queue_full / max(received + queue_full, 1), 6
            ),
            "duplicate_observation_rate": round(
                duplicates / max(received + duplicates, 1), 6
            ),
            "replay_observation_rate": round(
                replayed / max(received + replayed, 1), 6
            ),
        }

    @staticmethod
    def _safe_error_code(value: str) -> str:
        code = str(value or "unknown_error")
        if not code.replace("_", "").isalnum() or len(code) > 48:
            return "unknown_error"
        return code


_DEFAULT_STORES: dict[str, HeliusEventStore] = {}


def get_default_event_store() -> HeliusEventStore:
    """Return one shared, lazily initialized event store for this bot process."""
    import config

    path = str(Path(config.HELIUS_EVENT_QUEUE_PATH))
    store = _DEFAULT_STORES.get(path)
    if store is None:
        store = HeliusEventStore(
            path,
            max_pending_events=config.HELIUS_EVENT_QUEUE_MAX_PENDING,
            max_pending_bytes=config.HELIUS_EVENT_QUEUE_MAX_BYTES,
            retry_base_seconds=config.HELIUS_EVENT_RETRY_BASE_S,
            retry_max_seconds=config.HELIUS_EVENT_RETRY_MAX_S,
        )
        _DEFAULT_STORES[path] = store
    return store


async def process_next_event(
    store: HeliusEventStore,
    stream: str,
    handler: Callable[[EventRecord], Awaitable[None]],
) -> str:
    """Run one ordered event; retry pre-decision failures and block ambiguous effects."""
    event = store.claim_next(stream)
    if event is None:
        return "unknown" if store.has_unknown(stream) else "idle"

    if event.slot is None or event.transaction_index is None:
        store.mark_unknown(event.event_id, "canonical_order_missing")
        log.error("Helius event lacks provider-canonical order; ordered stream is blocked")
        record_execution_metric("event.unknown", 0, "unknown", "helius")
        return "unknown"

    with correlate_event(event.correlation_id):
        with measure_execution("event.processing", "helius") as metric:
            try:
                await handler(event)
            except RetryableEventError as exc:
                store.mark_retry(event.event_id, exc.code)
                metric["outcome"] = "failure"
                record_execution_metric("event.retry", 0, "failure", "helius")
                return "retry"
            except asyncio.CancelledError:
                store.mark_unknown(event.event_id, "worker_cancelled")
                metric["outcome"] = "unknown"
                record_execution_metric("event.unknown", 0, "unknown", "helius")
                raise
            except Exception:
                store.mark_unknown(event.event_id)
                metric["outcome"] = "unknown"
                record_execution_metric("event.unknown", 0, "unknown", "helius")
                log.error("Helius event outcome UNKNOWN; ordered stream is blocked")
                return "unknown"
            store.mark_processed(event.event_id)
            elapsed_ms = max(0.0, (time.time() - event.received_at) * 1000)
            record_execution_metric("event.end_to_end", elapsed_ms, "success", "helius")
            return "processed"


async def drain_stream(
    store: HeliusEventStore,
    stream: str,
    handler: Callable[[EventRecord], Awaitable[None]],
    *,
    max_events: int = 100,
) -> str:
    """Drain ready events in order without waiting through a retry delay."""
    for _ in range(max_events):
        result = await process_next_event(store, stream, handler)
        if result != "processed":
            return result
    return "batch_limit"


async def recover_required_streams(
    store: HeliusEventStore,
    prefix: str,
    handler: Callable[[EventRecord], Awaitable[None]],
    address_for_stream: Callable[[str], str | None],
) -> bool:
    """Drain persisted rows, replay any confirmed history gap, and unblock intake."""
    for stream in store.recovery_required_streams(prefix):
        if store.pending_count(stream):
            await drain_stream(store, stream, handler)
            if store.pending_count(stream):
                return False

        _, checkpoint_signature = store.checkpoint(stream)
        if checkpoint_signature is None:
            # Do not silently resume a previously active stream without a durable
            # cursor. A fresh stream is initialized without recovery_required;
            # once it has disconnected or encountered an error, missing history
            # cannot be bounded safely and needs operator review.
            store.mark_recovery_required(stream, "missing_checkpoint")
            log.error("Helius event recovery blocked: durable checkpoint is missing")
            return False

        address = address_for_stream(stream)
        if not address:
            log.error("Helius event recovery blocked: stream address is unavailable")
            return False
        try:
            await replay_stream_history(
                store, stream, address, fetch_signature_page
            )
        except EventRecoveryError:
            log.error("Helius event recovery blocked: history gap is unverified")
            return False
        await drain_stream(store, stream, handler)
        if not store.complete_recovery_if_idle(stream):
            return False
    return True


FetchSignaturePage = Callable[[str, str | None, int], Awaitable[list[dict]]]


async def replay_stream_history(
    store: HeliusEventStore,
    stream: str,
    address: str,
    fetch_page: FetchSignaturePage,
) -> int:
    """Replay an address-history gap only with a verifiable transaction order.

    The RPC provider returns newest-first pages. The checkpoint must be found
    before any replay is enqueued. If it is outside retained provider history or
    the gap exceeds inbox capacity, the stream remains paused for manual review.
    A slot and transaction index are required so page order can be checked
    across slots and within each slot before intake resumes.
    """
    if not isinstance(address, str) or not address.strip():
        raise EventRecoveryError("History replay requires a known monitored address")
    _, checkpoint_signature = store.checkpoint(stream)
    if not checkpoint_signature:
        raise EventRecoveryError("History replay has no confirmed checkpoint")

    newest_first: list[dict] = []
    before: str | None = None
    found_checkpoint = False
    previous_order: tuple[int, int] | None = None
    page_limit = _PAGE_LIMIT
    max_items = store.max_pending_events
    max_pages = (max_items // page_limit) + 2
    for _ in range(max_pages):
        try:
            page = await fetch_page(address, before, page_limit)
        except Exception:
            # Keep provider details, URLs, and exception text out of logs and
            # persisted event state. The stream remains paused and can retry
            # recovery on its next connection.
            raise EventRecoveryError(
                "History replay source is temporarily unavailable"
            ) from None
        if not isinstance(page, list):
            raise EventRecoveryError("History replay returned an invalid page")
        if not page:
            break
        for item in page:
            if not isinstance(item, dict):
                raise EventRecoveryError("History replay contains an invalid entry")
            signature = item.get("signature")
            if not isinstance(signature, str) or not signature:
                raise EventRecoveryError("History replay entry has no signature")
            slot = item.get("slot")
            transaction_index = item.get("transactionIndex")
            if type(slot) is not int or slot < 0 or type(transaction_index) is not int or transaction_index < 0:
                raise EventRecoveryError(
                    "History replay is missing canonical slot and transaction index"
                )
            order = (slot, transaction_index)
            if previous_order is not None and order >= previous_order:
                raise EventRecoveryError(
                    "History replay transaction ordering is not canonical"
                )
            previous_order = order
            if signature == checkpoint_signature:
                found_checkpoint = True
                break
            newest_first.append(item)
            if len(newest_first) > max_items:
                raise EventRecoveryError("History gap exceeds the configured inbox bound")
        if found_checkpoint:
            break
        if len(page) < page_limit:
            break
        next_before = page[-1].get("signature")
        if not isinstance(next_before, str) or next_before == before:
            raise EventRecoveryError("History replay pagination did not advance")
        before = next_before

    if not found_checkpoint:
        raise EventRecoveryError("History replay could not reach the durable checkpoint")

    inserted = 0
    for item in reversed(newest_first):
        result = store.enqueue(
            stream,
            item["signature"],
            {"replayed": True},
            slot=item["slot"],
            transaction_index=item["transactionIndex"],
            is_replay=True,
        )
        inserted += int(result.inserted)
    store.clear_intake_pause(stream)
    return inserted


async def fetch_signature_page(address: str, before: str | None, limit: int) -> list[dict]:
    """Fetch one read-only Solana history page through the idempotent RPC client."""
    import rpc_client

    config = {"limit": min(limit, _PAGE_LIMIT), "commitment": "confirmed"}
    if before:
        config["before"] = before
    response = await rpc_client.rpc_post_strict(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [address, config],
        },
        timeout=15,
    )
    if not isinstance(response, list):
        raise EventRecoveryError("History replay RPC returned an invalid response")
    return response
