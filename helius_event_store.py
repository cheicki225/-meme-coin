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
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from execution_metrics import (
    correlate_event,
    measure_execution,
    record_execution_metric,
)


log = logging.getLogger("helius_event_store")

_SCHEMA_VERSION = 7
_PAGE_LIMIT = 1000
_SUBSCRIPTION_ACK_TIMEOUT_SECONDS = 15.0
_SUBSCRIPTION_BUFFER_LIMIT = 1000
_FINALITY_POLL_SECONDS = 1.0
_FINALITY_RPC_FAILURE_LIMIT = 3
_ACTIVE_STATES = ("pending", "retry", "processing", "unknown")


class EventStoreError(RuntimeError):
    """Raised when the durable event inbox cannot safely read or write state."""


class EventQueueFullError(EventStoreError):
    """Raised when a notification cannot be durably added within configured bounds."""


class EventIntakePausedError(EventStoreError):
    """Raised when new live events arrive before a stream's history is recovered."""


class EventRecoveryError(EventStoreError):
    """Raised when history replay cannot prove the gap was completely recovered."""


class EventClaimLostError(EventStoreError):
    """Raised when a worker attempts to mutate an event after losing its claim."""


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
    claim_generation: int = 0
    recovery_generation: int = 0
    recovery_claim: bool = False


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
                # Refuse a newer schema before any persistent journal-mode change
                # or DDL can touch a database owned by a newer application.
                version = int(db.execute("PRAGMA user_version").fetchone()[0])
                if version > _SCHEMA_VERSION:
                    raise EventStoreError("Helius event inbox schema is newer than this bot")
                mode = db.execute("PRAGMA journal_mode = WAL").fetchone()
                if not mode or str(mode[0]).lower() != "wal":
                    raise EventStoreError("Helius event inbox could not enable WAL mode")
                integrity = db.execute("PRAGMA integrity_check").fetchall()
                if integrity != [("ok",)]:
                    raise EventStoreError("Helius event inbox integrity check failed")
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS event_streams (
                        stream TEXT PRIMARY KEY,
                        next_sequence INTEGER NOT NULL DEFAULT 0,
                        checkpoint_sequence INTEGER NOT NULL DEFAULT 0,
                        checkpoint_signature TEXT,
                        recovery_required INTEGER NOT NULL DEFAULT 0,
                        intake_paused INTEGER NOT NULL DEFAULT 0,
                        blocked_reason TEXT,
                        recovery_generation INTEGER NOT NULL DEFAULT 0,
                        recovery_boundary_slot INTEGER
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
                        claim_generation INTEGER NOT NULL DEFAULT 0,
                        claim_recovery_generation INTEGER NOT NULL DEFAULT 0,
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
                if "claim_generation" not in columns:
                    db.execute(
                        "ALTER TABLE event_inbox ADD COLUMN "
                        "claim_generation INTEGER NOT NULL DEFAULT 0"
                    )
                if "claim_recovery_generation" not in columns:
                    db.execute(
                        "ALTER TABLE event_inbox ADD COLUMN "
                        "claim_recovery_generation INTEGER NOT NULL DEFAULT 0"
                    )
                stream_columns = {
                    str(row[1]) for row in db.execute("PRAGMA table_info(event_streams)")
                }
                if "recovery_generation" not in stream_columns:
                    db.execute(
                        "ALTER TABLE event_streams ADD COLUMN "
                        "recovery_generation INTEGER NOT NULL DEFAULT 0"
                    )
                if "recovery_boundary_slot" not in stream_columns:
                    db.execute(
                        "ALTER TABLE event_streams ADD COLUMN recovery_boundary_slot INTEGER"
                    )
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
        if slot is not None and (
            type(slot) is not int or slot < 0 or slot >= 2**64
        ):
            raise ValueError("slot must be a non-negative integer")
        if transaction_index is not None and (
            type(transaction_index) is not int
            or transaction_index < 0
            or transaction_index >= 2**32
        ):
            raise ValueError("transaction_index must be a uint32")
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
                    "SELECT event_id, status, correlation_id, slot, transaction_index, "
                    "effect_started, last_error_code, payload_json FROM event_inbox "
                    "WHERE stream=? AND signature=?",
                    (stream, signature),
                ).fetchone()
                if existing:
                    self._counter(db, "duplicates")
                    existing_status = str(existing[1])
                    if is_replay:
                        self._counter(db, "replayed")
                        existing_slot = existing[3]
                        existing_index = existing[4]
                        if existing_slot is not None and int(existing_slot) != slot:
                            raise EventRecoveryError(
                                "History replay conflicts with the stored event slot"
                            )
                        if existing_index is not None and int(existing_index) != transaction_index:
                            raise EventRecoveryError(
                                "History replay conflicts with the stored transaction index"
                            )
                        missing_order = existing_slot is None or existing_index is None
                        can_reconcile_unknown = (
                            existing_status == "unknown"
                            and existing[6] == "canonical_order_missing"
                            and not bool(existing[5])
                        )
                        can_enrich_unclaimed = (
                            existing_status in {"pending", "retry"}
                            and not bool(existing[5])
                        )
                        if missing_order and (
                            can_reconcile_unknown or can_enrich_unclaimed
                        ):
                            try:
                                previous_payload = json.loads(existing[7])
                            except (json.JSONDecodeError, TypeError) as exc:
                                raise EventStoreError(
                                    "Helius event payload is corrupt during order reconciliation"
                                ) from exc
                            if not isinstance(previous_payload, dict):
                                raise EventStoreError(
                                    "Helius event payload is invalid during order reconciliation"
                                )
                            merged_payload = dict(previous_payload)
                            merged_payload.update(payload)
                            encoded_payload = json.dumps(
                                merged_payload,
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=True,
                                allow_nan=False,
                            )
                            new_status = "retry" if can_reconcile_unknown else existing_status
                            db.execute(
                                """
                                UPDATE event_inbox SET slot=?, transaction_index=?,
                                    payload_json=?, status=?, next_retry_at=CASE
                                        WHEN ?='retry' AND ?='unknown' THEN 0
                                        ELSE next_retry_at END,
                                    lease_until=CASE WHEN ?='unknown' THEN 0 ELSE lease_until END,
                                    last_error_code=CASE WHEN ?='unknown' THEN NULL
                                        ELSE last_error_code END
                                WHERE event_id=?
                                """,
                                (
                                    slot,
                                    transaction_index,
                                    encoded_payload,
                                    new_status,
                                    new_status,
                                    existing_status,
                                    existing_status,
                                    existing_status,
                                    int(existing[0]),
                                ),
                            )
                            existing_status = new_status
                    db.commit()
                    correlation_id = str(existing[2] or correlation_id)
                    result = EnqueueResult(
                        int(existing[0]), False, existing_status, correlation_id
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
                            blocked_reason='queue_full',
                            recovery_generation=recovery_generation+1,
                            recovery_boundary_slot=NULL WHERE stream=?
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

    def claim_next(
        self,
        stream: str,
        *,
        now: float | None = None,
        recovery: bool = False,
    ) -> EventRecord | None:
        """Claim the earliest canonically ordered unprocessed event in a stream.

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
                stream_state = db.execute(
                    "SELECT recovery_required, intake_paused, recovery_generation "
                    "FROM event_streams "
                    "WHERE stream=?",
                    (stream,),
                ).fetchone()
                if stream_state:
                    recovery_required = bool(stream_state[0])
                    intake_paused = bool(stream_state[1])
                    recovery_generation = int(stream_state[2])
                    if not recovery and (recovery_required or intake_paused):
                        return None
                    if recovery and (not recovery_required or intake_paused):
                        return None
                else:
                    recovery_generation = 0
                unknown = db.execute(
                    "SELECT 1 FROM event_inbox WHERE stream=? AND status='unknown' LIMIT 1",
                    (stream,),
                ).fetchone()
                if unknown:
                    return None
                active_claim = db.execute(
                    "SELECT 1 FROM event_inbox WHERE stream=? AND status='processing' "
                    "AND lease_until>? LIMIT 1",
                    (stream, current),
                ).fetchone()
                if active_claim:
                    return None
                row = db.execute(
                    """
                    SELECT event_id, sequence, signature, correlation_id, slot,
                           transaction_index,
                           payload_json, status,
                            attempts, next_retry_at, lease_until, received_at,
                            effect_started, claim_generation
                    FROM event_inbox
                    WHERE stream=? AND status != 'processed'
                    ORDER BY
                        CASE WHEN slot IS NULL OR transaction_index IS NULL THEN 0 ELSE 1 END,
                        slot, transaction_index, sequence
                    LIMIT 1
                    """,
                    (stream,),
                ).fetchone()
                if not row:
                    return None
                (
                    event_id, sequence, signature, correlation_id, slot,
                    transaction_index,
                    raw_payload, status, attempts, retry_at, lease_until, received_at,
                    effect_started, claim_generation,
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
                        "UPDATE event_streams SET recovery_required=1, intake_paused=1, "
                        "blocked_reason='lease_expired_after_effect', "
                        "recovery_generation=recovery_generation+1, "
                        "recovery_boundary_slot=NULL WHERE stream=?",
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
                        lease_until=?, next_retry_at=0, effect_started=0,
                        claim_generation=claim_generation+1,
                        claim_recovery_generation=? WHERE event_id=?
                    """,
                    (current + self.lease_seconds, recovery_generation, event_id),
                )
                self._counter(db, "claimed")
                return EventRecord(
                    int(event_id), stream, int(sequence), str(signature),
                    str(correlation_id or uuid.uuid4().hex), slot,
                    payload, int(attempts) + 1, float(received_at), transaction_index,
                    int(claim_generation) + 1, recovery_generation, recovery,
                )
        except sqlite3.DatabaseError as exc:
            raise EventStoreError("Helius inbox claim failed") from exc

    def mark_retry(
        self,
        event_id: int,
        error_code: str,
        *,
        claim_generation: int,
        recovery_generation: int | None = None,
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
                    "SELECT event_inbox.attempts, event_inbox.effect_started, "
                    "event_inbox.claim_generation, event_inbox.lease_until, "
                    "event_inbox.claim_recovery_generation, "
                    "event_streams.recovery_generation "
                    "FROM event_inbox JOIN event_streams "
                    "ON event_streams.stream=event_inbox.stream "
                    "WHERE event_id=? AND status='processing'",
                    (event_id,),
                ).fetchone()
                if (
                    not row
                    or int(row[2]) != claim_generation
                    or float(row[3]) <= current
                    or int(row[4]) != int(row[5])
                    or (
                        recovery_generation is not None
                        and int(row[4]) != recovery_generation
                    )
                ):
                    raise EventClaimLostError("Helius event claim is no longer active")
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

    def mark_effect_started(
        self,
        event_id: int,
        *,
        claim_generation: int,
        now: float | None = None,
        recovery: bool = False,
        recovery_generation: int | None = None,
    ) -> None:
        """Persist the boundary before invoking a callback with possible side effects."""
        current = time.time() if now is None else float(now)
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                stream_state = db.execute(
                    "SELECT event_inbox.claim_recovery_generation, "
                    "event_streams.recovery_generation, event_streams.recovery_required, "
                    "event_streams.intake_paused FROM event_inbox JOIN event_streams "
                    "ON event_streams.stream=event_inbox.stream "
                    "WHERE event_inbox.event_id=? AND event_inbox.status='processing' "
                    "AND event_inbox.claim_generation=?",
                    (event_id, claim_generation),
                ).fetchone()
                if not stream_state:
                    raise EventClaimLostError("Helius event claim is no longer active")
                claim_recovery_generation = int(stream_state[0])
                current_recovery_generation = int(stream_state[1])
                if (
                    claim_recovery_generation != current_recovery_generation
                    or (
                        recovery_generation is not None
                        and claim_recovery_generation != recovery_generation
                    )
                ):
                    raise EventClaimLostError("Helius event recovery claim is stale")
                recovery_required = bool(stream_state[2])
                intake_paused = bool(stream_state[3])
                if recovery:
                    available = recovery_required and not intake_paused
                else:
                    available = not recovery_required and not intake_paused
                if not available:
                    raise EventClaimLostError(
                        "Helius event stream is not available for new effects"
                    )
                cursor = db.execute(
                    "UPDATE event_inbox SET effect_started=1 WHERE event_id=? "
                    "AND status='processing' AND claim_generation=? AND lease_until>?",
                    (event_id, claim_generation, current),
                )
                if cursor.rowcount != 1:
                    raise EventClaimLostError("Helius event claim is no longer active")
        except sqlite3.DatabaseError as exc:
            raise EventStoreError("Helius effect boundary could not be persisted") from exc

    def mark_unknown(
        self,
        event_id: int,
        error_code: str = "handler_outcome_unknown",
        *,
        claim_generation: int,
    ) -> None:
        """Block the stream when downstream effects may have partially occurred."""
        code = self._safe_error_code(error_code)
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT stream, claim_generation FROM event_inbox "
                    "WHERE event_id=? AND status='processing'",
                    (event_id,),
                ).fetchone()
                if not row or int(row[1]) != claim_generation:
                    raise EventClaimLostError("Helius event claim is no longer active")
                db.execute(
                    """
                    UPDATE event_inbox SET status='unknown', lease_until=0,
                        last_error_code=? WHERE event_id=? AND claim_generation=?
                    """,
                    (code, event_id, claim_generation),
                )
                db.execute(
                    """
                    UPDATE event_streams SET recovery_required=1, intake_paused=1,
                        blocked_reason=?, recovery_generation=recovery_generation+1,
                        recovery_boundary_slot=NULL
                    WHERE stream=?
                    """,
                    (code, row[0]),
                )
                self._counter(db, "unknown")
        except sqlite3.DatabaseError as exc:
            raise EventStoreError("Helius UNKNOWN outcome could not be persisted") from exc

    def mark_processed(
        self,
        event_id: int,
        *,
        claim_generation: int,
        now: float | None = None,
        recovery: bool = False,
        recovery_generation: int | None = None,
    ) -> None:
        """Acknowledge only after the event handler has returned successfully."""
        processed_at = time.time() if now is None else float(now)
        checkpoint_blocked = False
        claim_lease_expired = False
        recovery_raced_ack = False
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT event_inbox.stream, event_inbox.sequence, "
                    "event_inbox.slot, event_inbox.transaction_index, "
                    "event_inbox.claim_generation, event_inbox.lease_until, "
                    "event_streams.recovery_required, event_streams.intake_paused "
                    ", event_inbox.claim_recovery_generation, "
                    "event_streams.recovery_generation "
                    "FROM event_inbox JOIN event_streams "
                    "ON event_streams.stream=event_inbox.stream "
                    "WHERE event_inbox.event_id=? AND event_inbox.status='processing'",
                    (event_id,),
                ).fetchone()
                if not row or int(row[4]) != claim_generation:
                    raise EventClaimLostError("Helius event claim is no longer active")
                stream = str(row[0])
                if float(row[5]) <= processed_at:
                    claim_lease_expired = True
                recovery_required = bool(row[6])
                intake_paused = bool(row[7])
                claim_recovery_generation = int(row[8])
                current_recovery_generation = int(row[9])
                recovery_epoch_mismatch = (
                    claim_recovery_generation != current_recovery_generation
                    or (
                        recovery_generation is not None
                        and claim_recovery_generation != recovery_generation
                    )
                )
                if recovery:
                    recovery_raced_ack = (
                        recovery_epoch_mismatch
                        or not recovery_required
                        or intake_paused
                    )
                else:
                    recovery_raced_ack = (
                        recovery_epoch_mismatch or recovery_required or intake_paused
                    )
                if row[2] is None or row[3] is None:
                    checkpoint_blocked = True
                checkpoint = db.execute(
                    "SELECT checkpoint_sequence FROM event_streams WHERE stream=?",
                    (stream,),
                ).fetchone()
                checkpoint_sequence = int(checkpoint[0]) if checkpoint else 0
                contiguous_sequence = checkpoint_sequence
                if not checkpoint_blocked and not claim_lease_expired and not recovery_raced_ack:
                    while True:
                        next_row = db.execute(
                            "SELECT sequence, status, slot, transaction_index "
                            "FROM event_inbox WHERE stream=? AND sequence=?",
                            (stream, contiguous_sequence + 1),
                        ).fetchone()
                        if not next_row:
                            break
                        next_is_current = int(next_row[0]) == int(row[1])
                        if next_row[1] != "processed" and not next_is_current:
                            break
                        if next_row[2] is None or next_row[3] is None:
                            checkpoint_blocked = True
                            break
                        contiguous_sequence = int(next_row[0])
                    if not checkpoint_blocked and checkpoint_sequence:
                        legacy_order = db.execute(
                            "SELECT 1 FROM event_inbox WHERE stream=? AND sequence<=? "
                            "AND status='processed' AND "
                            "(slot IS NULL OR transaction_index IS NULL) LIMIT 1",
                            (stream, checkpoint_sequence),
                        ).fetchone()
                        checkpoint_blocked = legacy_order is not None

                if claim_lease_expired or recovery_raced_ack:
                    error_code = (
                        "claim_lease_expired"
                        if claim_lease_expired
                        else "recovery_raced_ack"
                    )
                    db.execute(
                        "UPDATE event_inbox SET status='unknown', effect_started=1, "
                        "lease_until=0, last_error_code=? "
                        "WHERE event_id=? AND claim_generation=?",
                        (error_code, event_id, claim_generation),
                    )
                    db.execute(
                        "UPDATE event_streams SET recovery_required=1, intake_paused=1, "
                        "blocked_reason=?, recovery_generation=recovery_generation+1, "
                        "recovery_boundary_slot=NULL WHERE stream=?",
                        (error_code, stream),
                    )
                    self._counter(db, "unknown")
                elif checkpoint_blocked:
                    # The callback already returned, so do not make this row
                    # replayable. Preserve it as UNKNOWN and require operator
                    # reconciliation before advancing an untrustworthy cursor.
                    db.execute(
                        "UPDATE event_inbox SET status='unknown', effect_started=1, "
                        "lease_until=0, last_error_code='canonical_checkpoint_missing' "
                        "WHERE event_id=?",
                        (event_id,),
                    )
                    db.execute(
                        "UPDATE event_streams SET recovery_required=1, intake_paused=1, "
                        "blocked_reason='canonical_checkpoint_missing', "
                        "recovery_generation=recovery_generation+1, "
                        "recovery_boundary_slot=NULL WHERE stream=?",
                        (stream,),
                    )
                    self._counter(db, "unknown")
                else:
                    db.execute(
                        """
                        UPDATE event_inbox SET status='processed', payload_json='',
                            lease_until=0, processed_at=?, last_error_code=NULL
                        WHERE event_id=?
                        """,
                        (processed_at, event_id),
                    )
                    while True:
                        next_row = db.execute(
                            "SELECT sequence, status FROM event_inbox "
                            "WHERE stream=? AND sequence=?",
                            (stream, contiguous_sequence + 1),
                        ).fetchone()
                        if not next_row or next_row[1] != "processed":
                            break
                        contiguous_sequence = int(next_row[0])

                    checkpoint_signature = None
                    if contiguous_sequence:
                        latest = db.execute(
                            "SELECT signature FROM event_inbox WHERE stream=? "
                            "AND sequence<=? AND status='processed' "
                            "ORDER BY slot DESC, transaction_index DESC, sequence DESC LIMIT 1",
                            (stream, contiguous_sequence),
                        ).fetchone()
                        checkpoint_signature = str(latest[0]) if latest else None
                    db.execute(
                        """
                        UPDATE event_streams SET checkpoint_sequence=?, checkpoint_signature=?
                        WHERE stream=?
                        """,
                        (contiguous_sequence, checkpoint_signature, stream),
                    )
                    self._counter(db, "processed")
        except sqlite3.DatabaseError as exc:
            raise EventStoreError("Helius event acknowledgement failed") from exc
        if claim_lease_expired:
            raise EventClaimLostError("Helius event claim expired before acknowledgement")
        if recovery_raced_ack:
            raise EventRecoveryError(
                "Helius recovery began before the event acknowledgement"
            )
        if checkpoint_blocked:
            raise EventRecoveryError(
                "Canonical checkpoint is blocked by missing event order"
            )

    def checkpoint(self, stream: str) -> tuple[int, str | None]:
        stream = self._validate_stream(stream)
        with self._connect() as db:
            row = db.execute(
                "SELECT checkpoint_sequence, checkpoint_signature FROM event_streams WHERE stream=?",
                (stream,),
            ).fetchone()
        return (int(row[0]), row[1]) if row else (0, None)

    def checkpoint_order(self, stream: str) -> tuple[str, int, int] | None:
        """Return the stored canonical tuple for the durable replay boundary."""
        stream = self._validate_stream(stream)
        with self._connect() as db:
            checkpoint = db.execute(
                "SELECT checkpoint_sequence, checkpoint_signature FROM event_streams "
                "WHERE stream=?",
                (stream,),
            ).fetchone()
            if not checkpoint or not checkpoint[1]:
                return None
            row = db.execute(
                "SELECT signature, slot, transaction_index FROM event_inbox "
                "WHERE stream=? AND signature=? AND status='processed' AND sequence<=?",
                (stream, checkpoint[1], int(checkpoint[0])),
            ).fetchone()
            invalid_prefix = db.execute(
                "SELECT 1 FROM event_inbox WHERE stream=? AND sequence<=? "
                "AND status='processed' AND "
                "(slot IS NULL OR transaction_index IS NULL) LIMIT 1",
                (stream, int(checkpoint[0])),
            ).fetchone()
            latest = db.execute(
                "SELECT signature, slot, transaction_index FROM event_inbox "
                "WHERE stream=? AND sequence<=? AND status='processed' "
                "ORDER BY slot DESC, transaction_index DESC, sequence DESC LIMIT 1",
                (stream, int(checkpoint[0])),
            ).fetchone()
        if (
            not row
            or row[1] is None
            or row[2] is None
            or invalid_prefix
            or not latest
            or tuple(latest) != tuple(row)
        ):
            return None
        return (str(row[0]), int(row[1]), int(row[2]))

    def pending_count(self, stream: str | None = None) -> int:
        where, args = ("status != 'processed'", ()) if stream is None else (
            "status != 'processed' AND stream=?", (self._validate_stream(stream),)
        )
        with self._connect() as db:
            return int(db.execute(
                f"SELECT COUNT(*) FROM event_inbox WHERE {where}", args
            ).fetchone()[0])

    def needs_canonical_order_replay(self, stream: str) -> bool:
        """Return whether unprocessed rows lack a complete canonical order tuple."""
        stream = self._validate_stream(stream)
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM event_inbox WHERE stream=? AND status!='processed' "
                "AND (slot IS NULL OR transaction_index IS NULL) LIMIT 1",
                (stream,),
            ).fetchone()
        return row is not None

    def unknowns_are_order_only(self, stream: str) -> bool:
        """Allow replay enrichment only for no-effect canonical-order UNKNOWN rows."""
        stream = self._validate_stream(stream)
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM event_inbox WHERE stream=? AND status='unknown' "
                "AND (COALESCE(last_error_code,'')!='canonical_order_missing' "
                "OR effect_started!=0) LIMIT 1",
                (stream,),
            ).fetchone()
        return row is None

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
                INSERT INTO event_streams(
                    stream, recovery_required, intake_paused, blocked_reason,
                    recovery_generation, recovery_boundary_slot
                )
                VALUES(?,1,1,?,1,NULL)
                ON CONFLICT(stream) DO UPDATE SET
                    recovery_required=1, intake_paused=1, blocked_reason=excluded.blocked_reason,
                    recovery_generation=event_streams.recovery_generation+1,
                    recovery_boundary_slot=NULL
                """,
                (stream, code),
            )

    def recovery_state(self, stream: str) -> tuple[int, int | None] | None:
        """Return the current recovery epoch and its durable finality boundary."""
        stream = self._validate_stream(stream)
        with self._connect() as db:
            row = db.execute(
                "SELECT recovery_generation, recovery_boundary_slot "
                "FROM event_streams WHERE stream=? AND recovery_required=1",
                (stream,),
            ).fetchone()
        if row is None:
            return None
        boundary = int(row[1]) if row[1] is not None else None
        return int(row[0]), boundary

    def set_recovery_boundary(
        self, stream: str, *, recovery_generation: int, slot: int
    ) -> bool:
        """Persist a processed reconnect boundary only for the active recovery epoch."""
        stream = self._validate_stream(stream)
        if type(slot) is not int or slot < 0 or slot >= 2**64:
            raise ValueError("slot must be an unsigned 64-bit integer")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT recovery_generation, recovery_required, recovery_boundary_slot "
                "FROM event_streams WHERE stream=?",
                (stream,),
            ).fetchone()
            if row is None or not bool(row[1]) or int(row[0]) != recovery_generation:
                return False
            if row[2] is not None:
                return int(row[2]) == slot
            db.execute(
                "UPDATE event_streams SET recovery_boundary_slot=? "
                "WHERE stream=? AND recovery_generation=? AND recovery_required=1 "
                "AND recovery_boundary_slot IS NULL",
                (slot, stream, recovery_generation),
            )
            return db.execute("SELECT changes()").fetchone()[0] == 1

    def recovery_required_streams(self, prefix: str) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT stream FROM event_streams "
                "WHERE stream LIKE ? AND recovery_required=1 ORDER BY stream",
                (f"{prefix}%",),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def clear_intake_pause(self, stream: str, *, recovery_generation: int) -> None:
        stream = self._validate_stream(stream)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            recovery = db.execute(
                "SELECT recovery_required, recovery_generation FROM event_streams "
                "WHERE stream=?",
                (stream,),
            ).fetchone()
            if (
                recovery is None
                or not bool(recovery[0])
                or int(recovery[1]) != recovery_generation
            ):
                raise EventRecoveryError("Recovery changed while history was being validated")
            row = db.execute(
                "SELECT 1 FROM event_inbox WHERE stream=? AND status='unknown' LIMIT 1",
                (stream,),
            ).fetchone()
            if row:
                raise EventRecoveryError("UNKNOWN event blocks stream intake")
            db.execute(
                "UPDATE event_streams SET intake_paused=0 WHERE stream=? "
                "AND recovery_required=1 AND recovery_generation=?",
                (stream, recovery_generation),
            )

    def complete_recovery_if_idle(
        self, stream: str, *, recovery_generation: int
    ) -> bool:
        stream = self._validate_stream(stream)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            recovery = db.execute(
                "SELECT recovery_required, intake_paused, recovery_generation "
                "FROM event_streams WHERE stream=?",
                (stream,),
            ).fetchone()
            if (
                recovery is None
                or int(recovery[2]) != recovery_generation
                or bool(recovery[1])
            ):
                return False
            pending = db.execute(
                "SELECT 1 FROM event_inbox WHERE stream=? AND status != 'processed' LIMIT 1",
                (stream,),
            ).fetchone()
            if pending:
                return False
            if not bool(recovery[0]):
                return True
            db.execute(
                """
                UPDATE event_streams SET recovery_required=0, intake_paused=0,
                    blocked_reason=NULL WHERE stream=? AND recovery_generation=?
                    AND recovery_required=1 AND intake_paused=0
                """,
                (stream, recovery_generation),
            )
            return db.execute("SELECT changes()").fetchone()[0] == 1

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
    *,
    recovery: bool = False,
) -> str:
    """Run one ordered event; retry pre-decision failures and block ambiguous effects."""
    event = store.claim_next(stream, recovery=recovery)
    if event is None:
        return "unknown" if store.has_unknown(stream) else "idle"

    if event.slot is None or event.transaction_index is None:
        try:
            store.mark_unknown(
                event.event_id,
                "canonical_order_missing",
                claim_generation=event.claim_generation,
            )
        except EventClaimLostError:
            return "unknown" if store.has_unknown(stream) else "lease_lost"
        log.error("Helius event lacks provider-canonical order; ordered stream is blocked")
        record_execution_metric("event.unknown", 0, "unknown", "helius")
        return "unknown"

    with correlate_event(event.correlation_id):
        with measure_execution("event.processing", "helius") as metric:
            try:
                await handler(event)
            except RetryableEventError as exc:
                try:
                    store.mark_retry(
                        event.event_id,
                        exc.code,
                        claim_generation=event.claim_generation,
                        recovery_generation=event.recovery_generation,
                    )
                except EventClaimLostError:
                    metric["outcome"] = "skipped"
                    return "unknown" if store.has_unknown(stream) else "lease_lost"
                metric["outcome"] = "failure"
                record_execution_metric("event.retry", 0, "failure", "helius")
                return "retry"
            except asyncio.CancelledError:
                try:
                    store.mark_unknown(
                        event.event_id,
                        "worker_cancelled",
                        claim_generation=event.claim_generation,
                    )
                except EventClaimLostError:
                    pass
                metric["outcome"] = "unknown"
                record_execution_metric("event.unknown", 0, "unknown", "helius")
                raise
            except EventClaimLostError:
                metric["outcome"] = "skipped"
                return "unknown" if store.has_unknown(stream) else "lease_lost"
            except Exception:
                try:
                    store.mark_unknown(
                        event.event_id,
                        claim_generation=event.claim_generation,
                    )
                except EventClaimLostError:
                    metric["outcome"] = "skipped"
                    return "unknown" if store.has_unknown(stream) else "lease_lost"
                metric["outcome"] = "unknown"
                record_execution_metric("event.unknown", 0, "unknown", "helius")
                log.error("Helius event outcome UNKNOWN; ordered stream is blocked")
                return "unknown"
            try:
                store.mark_processed(
                    event.event_id,
                    claim_generation=event.claim_generation,
                    recovery=event.recovery_claim,
                    recovery_generation=event.recovery_generation,
                )
            except EventClaimLostError:
                metric["outcome"] = "skipped"
                return "unknown" if store.has_unknown(stream) else "lease_lost"
            except EventRecoveryError:
                metric["outcome"] = "unknown"
                record_execution_metric("event.unknown", 0, "unknown", "helius")
                log.error("Helius checkpoint order is unverified; stream remains blocked")
                return "unknown"
            elapsed_ms = max(0.0, (time.time() - event.received_at) * 1000)
            record_execution_metric("event.end_to_end", elapsed_ms, "success", "helius")
            return "processed"


async def drain_stream(
    store: HeliusEventStore,
    stream: str,
    handler: Callable[[EventRecord], Awaitable[None]],
    *,
    max_events: int = 100,
    recovery: bool = False,
    should_stop: Callable[[], bool] | None = None,
) -> str:
    """Drain ready events in order without waiting through a retry delay."""
    for _ in range(max_events):
        if should_stop is not None and should_stop():
            return "stopped"
        result = await process_next_event(store, stream, handler, recovery=recovery)
        if result != "processed":
            return result
    return "batch_limit"


async def recover_required_streams(
    store: HeliusEventStore,
    prefix: str,
    handler: Callable[[EventRecord], Awaitable[None]],
    address_for_stream: Callable[[str], str | None],
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """Wait for reconnect finality, replay the address gap, and then drain it."""
    for stream in store.recovery_required_streams(prefix):
        if should_stop is not None and should_stop():
            return False
        if store.has_unknown(stream) and not store.unknowns_are_order_only(stream):
            return False

        _, checkpoint_signature = store.checkpoint(stream)
        if checkpoint_signature is None:
            store.mark_recovery_required(stream, "missing_checkpoint")
            log.error("Helius event recovery blocked: durable checkpoint is missing")
            return False
        address = address_for_stream(stream)
        if not address:
            log.error("Helius event recovery blocked: stream address is unavailable")
            return False

        recovery_state = store.recovery_state(stream)
        if recovery_state is None:
            return False
        recovery_generation, boundary_slot = recovery_state
        if boundary_slot is None:
            try:
                boundary_slot = await fetch_processed_slot()
            except Exception:
                log.error("Helius event recovery blocked: processed reconnect boundary is unavailable")
                return False
            if not store.set_recovery_boundary(
                stream,
                recovery_generation=recovery_generation,
                slot=boundary_slot,
            ):
                return False

        next_wait_log = time.monotonic()
        finalized_rpc_failures = 0
        while True:
            if should_stop is not None and should_stop():
                return False
            current_recovery = store.recovery_state(stream)
            if current_recovery is None or current_recovery[0] != recovery_generation:
                return False
            try:
                finalized_slot = await fetch_finalized_slot()
            except EventRecoveryError:
                log.error("Helius event recovery blocked: finalized slot response is invalid")
                return False
            except Exception:
                finalized_rpc_failures += 1
                if finalized_rpc_failures >= _FINALITY_RPC_FAILURE_LIMIT:
                    log.error(
                        "Helius event recovery paused after repeated finalized-slot failures"
                    )
                    return False
                finalized_slot = -1
            else:
                finalized_rpc_failures = 0
            if finalized_slot >= boundary_slot:
                break
            if time.monotonic() >= next_wait_log:
                log.warning(
                    "Helius recovery is waiting for finalized history to reach "
                    "the persisted reconnect boundary"
                )
                next_wait_log = time.monotonic() + 30.0
            await asyncio.sleep(_FINALITY_POLL_SECONDS)

        for _attempt in range(3):
            if should_stop is not None and should_stop():
                return False
            try:
                inserted = await replay_stream_history(
                    store,
                    stream,
                    address,
                    fetch_signature_page,
                    recovery_generation=recovery_generation,
                )
            except EventRecoveryError as exc:
                log.error("Helius event recovery blocked: %s", str(exc))
                return False
            if should_stop is not None and should_stop():
                return False
            if store.has_unknown(stream) or store.needs_canonical_order_replay(stream):
                return False
            try:
                store.clear_intake_pause(
                    stream, recovery_generation=recovery_generation
                )
            except EventRecoveryError:
                return False
            if should_stop is not None and should_stop():
                return False
            if store.pending_count(stream):
                await drain_stream(
                    store,
                    stream,
                    handler,
                    recovery=True,
                    should_stop=should_stop,
                )
                if should_stop is not None and should_stop():
                    return False
                if store.pending_count(stream):
                    return False
            if store.has_unknown(stream):
                return False
            if inserted == 0 and store.complete_recovery_if_idle(
                stream, recovery_generation=recovery_generation
            ):
                break
        else:
            return False
    return True


FetchSignaturePage = Callable[[str, str | None, int], Awaitable[list[dict]]]
async def replay_stream_history(
    store: HeliusEventStore,
    stream: str,
    address: str,
    fetch_page: FetchSignaturePage,
    *,
    recovery_generation: int | None = None,
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
    recovery_state = store.recovery_state(stream)
    if recovery_generation is None:
        if recovery_state is None:
            raise EventRecoveryError("History replay requires an active recovery epoch")
        recovery_generation = recovery_state[0]
    _, checkpoint_signature = store.checkpoint(stream)
    if not checkpoint_signature:
        raise EventRecoveryError("History replay has no confirmed checkpoint")
    checkpoint_order = store.checkpoint_order(stream)
    if checkpoint_order is None or checkpoint_order[0] != checkpoint_signature:
        raise EventRecoveryError(
            "History replay has no canonical durable checkpoint order"
        )

    newest_first: list[dict] = []
    before: str | None = None
    found_checkpoint = False
    previous_order: tuple[int, int] | None = None
    seen_signatures: set[str] = set()
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
            if signature in seen_signatures:
                raise EventRecoveryError("History replay contains a duplicate signature")
            seen_signatures.add(signature)
            slot = item.get("slot")
            transaction_index = item.get("transactionIndex")
            if (
                type(slot) is not int
                or slot < 0
                or slot >= 2**64
                or type(transaction_index) is not int
                or transaction_index < 0
                or transaction_index >= 2**32
            ):
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
                if order != (checkpoint_order[1], checkpoint_order[2]):
                    raise EventRecoveryError(
                        "History replay conflicts with the durable checkpoint order"
                    )
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
    current_recovery = store.recovery_state(stream)
    if current_recovery is None or current_recovery[0] != recovery_generation:
        raise EventRecoveryError("Recovery changed while history was being fetched")

    inserted = 0
    order_evidence = {
        "source": "solana_rpc_getSignaturesForAddress",
        "commitment": "finalized",
        "trust": "provider_reported",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    for item in reversed(newest_first):
        result = store.enqueue(
            stream,
            item["signature"],
            {"replayed": True, "canonical_order_evidence": order_evidence},
            slot=item["slot"],
            transaction_index=item["transactionIndex"],
            is_replay=True,
        )
        inserted += int(result.inserted)
    return inserted


async def _fetch_slot(commitment: str) -> int:
    """Read a Solana slot at one explicit commitment through the read-only RPC client."""
    if commitment not in {"processed", "finalized"}:
        raise ValueError("Unsupported slot commitment")
    import rpc_client

    response = await rpc_client.rpc_post_strict(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSlot",
            "params": [{"commitment": commitment}],
        },
        timeout=12,
    )
    if type(response) is not int or response < 0 or response >= 2**64:
        raise EventRecoveryError("Slot RPC returned an invalid boundary")
    return response


async def fetch_processed_slot() -> int:
    """Read the latest processed slot after the WebSocket subscriptions are active."""
    return await _fetch_slot("processed")


async def fetch_finalized_slot() -> int:
    """Read the highest finalized slot through the configured read-only RPC client."""
    return await _fetch_slot("finalized")


async def await_subscription_acknowledgements(
    websocket,
    pending_requests: dict[int, object],
    acknowledge: Callable[[str], Awaitable[None]],
) -> list[str]:
    """Wait for initial subscription ACKs and retain interleaved notifications.

    The reconnect boundary is taken only after every initial subscription is
    acknowledged. Notifications arriving during that handshake are buffered
    for delivery after history recovery; the buffer is deliberately bounded.
    """
    pending_ids = set(pending_requests)
    buffered: list[str] = []
    while pending_ids:
        try:
            message = await asyncio.wait_for(
                websocket.recv(), timeout=_SUBSCRIPTION_ACK_TIMEOUT_SECONDS
            )
        except Exception:
            raise EventRecoveryError(
                "WebSocket subscription acknowledgement is unavailable"
            ) from None
        if not isinstance(message, str):
            raise EventRecoveryError("WebSocket returned an invalid subscription frame")
        try:
            data = json.loads(message)
        except (TypeError, ValueError):
            raise EventRecoveryError("WebSocket returned invalid JSON during subscription") from None
        request_id = data.get("id") if isinstance(data, dict) else None
        if request_id in pending_ids:
            if type(request_id) is not int:
                raise EventRecoveryError(
                    "WebSocket subscription acknowledgement ID is invalid"
                )
            if (
                data.get("jsonrpc") != "2.0"
                or "error" in data
                or type(data.get("result")) is not int
            ):
                raise EventRecoveryError(
                    "WebSocket subscription acknowledgement was rejected"
                )
            await acknowledge(message)
            if request_id in pending_requests:
                raise EventRecoveryError("WebSocket subscription acknowledgement was rejected")
            pending_ids.remove(request_id)
            continue
        if len(buffered) >= _SUBSCRIPTION_BUFFER_LIMIT:
            raise EventRecoveryError("WebSocket pre-recovery notification buffer is full")
        buffered.append(message)
    return buffered


async def fetch_signature_page(address: str, before: str | None, limit: int) -> list[dict]:
    """Fetch one read-only Solana history page through the idempotent RPC client."""
    import rpc_client

    config = {"limit": min(limit, _PAGE_LIMIT), "commitment": "finalized"}
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
