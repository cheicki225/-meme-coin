"""Atomic, SQLite-backed PAPER admission primitives for Mission 18.

The store shares the bot-state SQLite file so admission snapshots, exposure
reservations, and position persistence can use the same writer transaction.
This module does not provide provider provenance or enable a runtime caller.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from typing import Iterator

import risk_controls
from mission13_admission import (
    PaperAdmissionDecision,
    PaperCandidateRequest,
    PaperSignalLedger,
    evaluate_paper_candidate,
)
from sqlite_state import SCHEMA_VERSION, SQLiteStateStore


class PaperAdmissionStoreError(RuntimeError):
    """Raised when the durable PAPER admission state cannot be trusted."""


class PaperAdmissionConflict(PaperAdmissionStoreError):
    """Raised when the state changed before an admitted position was committed."""


@dataclass(frozen=True)
class PaperAdmissionSnapshot:
    """Persisted state and active reservation totals used for one decision."""

    state: dict
    revision: int
    reserved_exposure_usd: float
    reserved_positions: int


@dataclass(frozen=True)
class PaperCandidatePolicyEvaluation:
    """Policy result that is explicitly blocked from runtime admission.

    Provider provenance and trusted wall-clock freshness are unavailable in
    this offline adapter. Even a positive policy result is not a permit and
    cannot create a durable reservation.
    """

    policy_decision: PaperAdmissionDecision
    admission_status: str = "blocked"
    provider_provenance_status: str = "UNKNOWN"
    time_basis: str = "caller_supplied_unverified"
    runtime_permitted: bool = False
    reservation_id: None = None


def _finite_positive(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value)) and value > 0
    except (OverflowError, TypeError, ValueError):
        return False


def _finite_nonnegative(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value)) and value >= 0
    except (OverflowError, TypeError, ValueError):
        return False


def _valid_key(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value.strip()) <= 200


class SQLitePaperAdmissionStore:
    """Offline persistence primitive for PAPER identity and exposure records.

    Pending and unknown rows continue to reserve exposure after restart. A
    completed row is committed in the same SQLite transaction as its position
    in ``bot_state``. Released identities remain as tombstones to prevent replay.
    These methods do not authenticate source provenance and are not an
    authenticated runtime admission route.
    """

    _ACTIVE_STATUSES = ("pending", "unknown")
    _STATUSES = ("pending", "unknown", "committed", "released")

    def __init__(self, path: str | Path, *, timeout_s: float = 10.0) -> None:
        if (
            not isinstance(timeout_s, (int, float))
            or isinstance(timeout_s, bool)
            or not _finite_positive(timeout_s)
        ):
            raise ValueError("invalid_paper_admission_timeout")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout_s = float(timeout_s)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.timeout_s)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout_s * 1000)}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        try:
            with self._connection() as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                version_row = connection.execute("PRAGMA user_version").fetchone()
                if not version_row or type(version_row[0]) is not int:
                    raise PaperAdmissionStoreError("paper_state_schema_invalid")
                database_version = version_row[0]
                state_tables = {"bot_state", "bot_state_revision"}
                if tables & state_tables:
                    if not state_tables.issubset(tables):
                        raise PaperAdmissionStoreError("paper_state_schema_unavailable")
                    # Validate a present state database before WAL setup or DDL.
                    self._read_state(connection)
                elif database_version != 0 or tables - {"mission18_paper_reservations"}:
                    raise PaperAdmissionStoreError("paper_state_schema_unsupported")

                reservation_table = "mission18_paper_reservations"
                if reservation_table in tables:
                    columns = {
                        str(row[1])
                        for row in connection.execute(
                            f"PRAGMA table_info({reservation_table})"
                        )
                    }
                    required_columns = {
                        "signal_id", "correlation_key", "exposure_usd", "status",
                        "created_at_utc", "updated_at_utc",
                    }
                    if not required_columns.issubset(columns):
                        raise PaperAdmissionStoreError("paper_admission_schema_unsupported")

                journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
                if not journal_mode or str(journal_mode[0]).lower() != "wal":
                    raise PaperAdmissionStoreError("paper_admission_wal_unavailable")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mission18_paper_reservations (
                        signal_id TEXT PRIMARY KEY,
                        correlation_key TEXT NOT NULL UNIQUE,
                        exposure_usd REAL NOT NULL CHECK (exposure_usd > 0),
                        status TEXT NOT NULL CHECK (
                            status IN ('pending', 'unknown', 'committed', 'released')
                        ),
                        created_at_utc TEXT NOT NULL,
                        updated_at_utc TEXT NOT NULL
                    )
                    """
                )
                connection.commit()
        except sqlite3.Error as exc:
            raise PaperAdmissionStoreError("paper_admission_schema_unavailable") from exc

    @staticmethod
    def _decode_state(payload: object) -> dict:
        if not isinstance(payload, str):
            raise PaperAdmissionStoreError("paper_state_payload_unavailable")

        def reject_constant(value: str):
            raise ValueError(f"invalid_json_constant:{value}")

        def reject_duplicates(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate_json_key")
                result[key] = value
            return result

        try:
            state = json.loads(
                payload,
                parse_constant=reject_constant,
                object_pairs_hook=reject_duplicates,
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise PaperAdmissionStoreError("paper_state_payload_invalid") from exc
        if not isinstance(state, dict):
            raise PaperAdmissionStoreError("paper_state_payload_invalid")
        return state

    @staticmethod
    def _read_state(connection: sqlite3.Connection) -> tuple[dict, int]:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"bot_state", "bot_state_revision"}.issubset(tables):
            raise PaperAdmissionStoreError("paper_state_schema_unavailable")
        state_row = connection.execute(
            "SELECT schema_version, payload FROM bot_state WHERE singleton=1"
        ).fetchone()
        revision_row = connection.execute(
            "SELECT revision FROM bot_state_revision WHERE singleton=1"
        ).fetchone()
        if state_row is None or revision_row is None:
            raise PaperAdmissionStoreError("paper_state_snapshot_unavailable")
        database_version = connection.execute("PRAGMA user_version").fetchone()
        if (
            not database_version
            or type(database_version[0]) is not int
            or database_version[0] != SCHEMA_VERSION
            or type(state_row["schema_version"]) is not int
            or state_row["schema_version"] != SCHEMA_VERSION
        ):
            raise PaperAdmissionStoreError("paper_state_schema_unsupported")
        if type(revision_row["revision"]) is not int or revision_row["revision"] < 0:
            raise PaperAdmissionStoreError("paper_state_revision_invalid")
        return SQLitePaperAdmissionStore._decode_state(state_row["payload"]), revision_row["revision"]

    @classmethod
    def _active_reservations(
        cls, connection: sqlite3.Connection
    ) -> tuple[float, int]:
        rows = connection.execute(
            "SELECT exposure_usd, status FROM mission18_paper_reservations"
        ).fetchall()
        exposure = 0.0
        count = 0
        for row in rows:
            value, status = row["exposure_usd"], row["status"]
            if not _finite_positive(value) or status not in cls._STATUSES:
                raise PaperAdmissionStoreError("paper_reservation_record_invalid")
            if status in cls._ACTIVE_STATUSES:
                exposure += float(value)
                count += 1
        if not math.isfinite(exposure):
            raise PaperAdmissionStoreError("paper_reservation_exposure_invalid")
        return exposure, count

    def snapshot(self) -> PaperAdmissionSnapshot:
        """Read the latest committed bot state and pending exposure totals."""
        try:
            with self._connection() as connection:
                connection.execute("BEGIN")
                state, revision = self._read_state(connection)
                reserved_exposure, reserved_positions = self._active_reservations(connection)
                connection.commit()
                return PaperAdmissionSnapshot(
                    state=state,
                    revision=revision,
                    reserved_exposure_usd=reserved_exposure,
                    reserved_positions=reserved_positions,
                )
        except PaperAdmissionStoreError:
            raise
        except sqlite3.Error as exc:
            raise PaperAdmissionStoreError("paper_admission_snapshot_unavailable") from exc

    @staticmethod
    def _validate_risk_inputs(
        *,
        proposed_exposure_usd: object,
        max_exposure_usd: object,
        max_open_positions: object,
        max_session_loss_usd: object,
    ) -> str | None:
        if not _finite_positive(proposed_exposure_usd):
            return "invalid_proposed_exposure"
        if not _finite_positive(max_exposure_usd):
            return "invalid_max_exposure_limit"
        if type(max_open_positions) is not int or max_open_positions <= 0:
            return "invalid_max_open_positions_limit"
        if not _finite_positive(max_session_loss_usd):
            return "invalid_session_loss_limit"
        return None

    def reserve_entry(
        self,
        signal_id: str,
        correlation_key: str,
        proposed_exposure_usd: float,
        *,
        max_exposure_usd: float,
        max_open_positions: int,
        max_session_loss_usd: float,
    ) -> tuple[bool, str | None]:
        """Atomically check persisted risk state and reserve identity/exposure."""
        if not _valid_key(signal_id) or not _valid_key(correlation_key):
            return False, "invalid_signal_identity"
        input_error = self._validate_risk_inputs(
            proposed_exposure_usd=proposed_exposure_usd,
            max_exposure_usd=max_exposure_usd,
            max_open_positions=max_open_positions,
            max_session_loss_usd=max_session_loss_usd,
        )
        if input_error:
            return False, input_error

        signal_id, correlation_key = signal_id.strip(), correlation_key.strip()
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                state, _ = self._read_state(connection)
                if "emergency_stop" not in state or type(state["emergency_stop"]) is not bool:
                    connection.rollback()
                    return False, "invalid_emergency_stop_state"
                if state["emergency_stop"]:
                    connection.rollback()
                    return False, "emergency_stop_active"
                losses = state.get("session_losses_usd")
                if not _finite_nonnegative(losses):
                    connection.rollback()
                    return False, "invalid_session_loss_state"
                if not isinstance(losses, (int, float)) or isinstance(losses, bool):
                    connection.rollback()
                    return False, "invalid_session_loss_state"
                if float(losses) >= float(max_session_loss_usd):
                    connection.rollback()
                    return False, "session_loss_limit_reached"

                duplicate = connection.execute(
                    "SELECT signal_id FROM mission18_paper_reservations WHERE signal_id=?",
                    (signal_id,),
                ).fetchone()
                if duplicate:
                    connection.rollback()
                    return False, "duplicate_signal"
                correlated = connection.execute(
                    "SELECT signal_id FROM mission18_paper_reservations WHERE correlation_key=?",
                    (correlation_key,),
                ).fetchone()
                if correlated:
                    connection.rollback()
                    return False, "correlated_signal"

                reserved_exposure, reserved_positions = self._active_reservations(connection)
                allowed, reason = risk_controls.assess_new_position(
                    state,
                    float(proposed_exposure_usd),
                    max_exposure_usd=float(max_exposure_usd),
                    max_open_positions=max_open_positions,
                    reserved_exposure_usd=reserved_exposure,
                    reserved_positions=reserved_positions,
                )
                if not allowed:
                    connection.rollback()
                    return False, reason or "risk_control_rejected"

                connection.execute(
                    "INSERT INTO mission18_paper_reservations "
                    "(signal_id, correlation_key, exposure_usd, status, created_at_utc, updated_at_utc) "
                    "VALUES (?, ?, ?, 'pending', ?, ?)",
                    (signal_id, correlation_key, float(proposed_exposure_usd), now, now),
                )
                connection.commit()
                return True, None
        except PaperAdmissionStoreError as exc:
            return False, str(exc)
        except sqlite3.IntegrityError:
            return False, "reservation_identity_conflict"
        except sqlite3.Error:
            return False, "paper_admission_storage_unavailable"

    def mark_unknown(self, signal_id: str) -> bool:
        """Keep an unresolved reservation active until an explicit reconciliation."""
        if not _valid_key(signal_id):
            return False
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "UPDATE mission18_paper_reservations SET status='unknown', updated_at_utc=? "
                    "WHERE signal_id=? AND status='pending'",
                    (datetime.now(timezone.utc).isoformat(timespec="microseconds"), signal_id.strip()),
                )
                connection.commit()
                return cursor.rowcount == 1
        except sqlite3.Error:
            return False

    def release_entry(self, signal_id: str, *, effect_not_started: bool) -> bool:
        """Fail closed because a caller assertion is not durable evidence.

        Pending or unknown exposure can only be resolved by a future
        reconciliation flow with persisted evidence. Until then, it stays
        reserved across restarts.
        """
        del signal_id, effect_not_started
        return False

    def commit_position(
        self,
        signal_id: str,
        state_after: dict,
        *,
        expected_revision: int,
    ) -> int:
        """Persist one PAPER position and commit its reservation atomically.

        ``state_after`` must be the current persisted state plus one appended
        position carrying ``mission18_signal_id``. Revision mismatches preserve
        the pending reservation for explicit recovery.
        """
        if not _valid_key(signal_id):
            raise PaperAdmissionStoreError("invalid_signal_identity")
        if type(expected_revision) is not int or expected_revision < 0:
            raise PaperAdmissionStoreError("invalid_expected_state_revision")
        if not isinstance(state_after, dict):
            raise PaperAdmissionStoreError("invalid_position_state")
        after_positions = state_after.get("open_positions")
        if not isinstance(after_positions, list) or not after_positions:
            raise PaperAdmissionStoreError("position_append_required")
        position = after_positions[-1]
        if not isinstance(position, dict) or position.get("mission18_signal_id") != signal_id:
            raise PaperAdmissionStoreError("position_signal_identity_mismatch")
        cost_basis = position.get("cost_basis_usd")
        if not _finite_positive(cost_basis):
            raise PaperAdmissionStoreError("invalid_position_exposure")

        try:
            encoded_state = SQLiteStateStore._encode_state(state_after)
        except Exception as exc:
            raise PaperAdmissionStoreError("position_state_not_serializable") from exc

        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current_state, revision = self._read_state(connection)
                emergency_stop = current_state.get("emergency_stop")
                if type(emergency_stop) is not bool:
                    connection.rollback()
                    raise PaperAdmissionStoreError("invalid_emergency_stop_state")
                if emergency_stop:
                    connection.rollback()
                    raise PaperAdmissionStoreError("emergency_stop_active")
                row = connection.execute(
                    "SELECT exposure_usd, status FROM mission18_paper_reservations WHERE signal_id=?",
                    (signal_id.strip(),),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    raise PaperAdmissionStoreError("paper_reservation_not_found")
                if row["status"] == "committed":
                    existing = current_state.get("open_positions")
                    if isinstance(existing, list) and any(
                        isinstance(item, dict)
                        and item.get("mission18_signal_id") == signal_id.strip()
                        for item in existing
                    ):
                        connection.commit()
                        return revision
                    connection.rollback()
                    raise PaperAdmissionStoreError("committed_paper_position_missing")
                if row["status"] != "pending":
                    connection.rollback()
                    raise PaperAdmissionStoreError("paper_reservation_not_pending")
                if cost_basis > row["exposure_usd"]:
                    connection.rollback()
                    raise PaperAdmissionStoreError("position_exceeds_reserved_exposure")
                if revision != expected_revision:
                    connection.rollback()
                    raise PaperAdmissionConflict("paper_state_revision_changed")

                current_positions = current_state.get("open_positions")
                if not isinstance(current_positions, list):
                    connection.rollback()
                    raise PaperAdmissionStoreError("invalid_persisted_position_state")
                if len(after_positions) != len(current_positions) + 1 or after_positions[:-1] != current_positions:
                    connection.rollback()
                    raise PaperAdmissionConflict("paper_position_snapshot_changed")
                if any(
                    isinstance(item, dict)
                    and item.get("mission18_signal_id") == signal_id.strip()
                    for item in current_positions
                ):
                    connection.rollback()
                    raise PaperAdmissionStoreError("duplicate_paper_position")

                for key in set(current_state) | set(state_after):
                    if key != "open_positions" and current_state.get(key) != state_after.get(key):
                        connection.rollback()
                        raise PaperAdmissionConflict("paper_state_snapshot_changed")

                new_revision = revision + 1
                connection.execute(
                    "UPDATE bot_state SET payload=?, updated_at=CURRENT_TIMESTAMP WHERE singleton=1",
                    (encoded_state,),
                )
                connection.execute(
                    "UPDATE bot_state_revision SET revision=? WHERE singleton=1",
                    (new_revision,),
                )
                connection.execute(
                    "UPDATE mission18_paper_reservations SET status='committed', updated_at_utc=? "
                    "WHERE signal_id=? AND status='pending'",
                    (datetime.now(timezone.utc).isoformat(timespec="microseconds"), signal_id.strip()),
                )
                connection.commit()
                return new_revision
        except PaperAdmissionStoreError:
            raise
        except sqlite3.Error as exc:
            raise PaperAdmissionStoreError("paper_position_commit_failed") from exc

    def counts(self) -> dict[str, int]:
        """Return aggregate reservation counts without exposing identities."""
        counts = {status: 0 for status in self._STATUSES}
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT status, COUNT(*) AS count FROM mission18_paper_reservations "
                    "GROUP BY status"
                ).fetchall()
        except sqlite3.Error as exc:
            raise PaperAdmissionStoreError("paper_reservation_counts_unavailable") from exc
        for row in rows:
            status = row["status"]
            count = row["count"]
            if status not in counts or type(count) is not int or count < 0:
                raise PaperAdmissionStoreError("paper_reservation_counts_invalid")
            counts[status] = count
        return counts


def evaluate_paper_candidate_policy_offline(
    request: PaperCandidateRequest,
    *,
    mode: str,
    as_of: datetime,
    position_state: dict,
    proposed_exposure_usd: float,
    configured_max_exposure_usd: float,
    configured_max_open_positions: int,
    configured_max_session_loss_usd: float,
) -> PaperCandidatePolicyEvaluation:
    """Evaluate supplied policy inputs without creating an admission permit.

    The caller's timestamp, observations, and provider provenance are not
    independently authenticated here. This offline helper is useful for
    deterministic policy inspection only; it cannot reserve exposure or
    authorize a runtime action.
    """
    assessed_at = as_of if isinstance(as_of, datetime) else datetime.now(timezone.utc)
    if (
        not isinstance(request, PaperCandidateRequest)
        or not isinstance(position_state, dict)
        or not isinstance(as_of, datetime)
        or as_of.tzinfo is None
        or as_of.utcoffset() is None
    ):
        decision = PaperAdmissionDecision(
            status="rejected",
            reason_codes=("invalid_policy_evaluation_input",),
            signal_id=(request.signal_id if isinstance(request, PaperCandidateRequest) else ""),
            correlation_key=(request.correlation_key if isinstance(request, PaperCandidateRequest) else ""),
            score=None,
            profile_id=None,
            regime="unknown",
            strategy_id=None,
            assessed_at=assessed_at,
        )
    else:
        decision = evaluate_paper_candidate(
            request,
            mode=mode,
            as_of=as_of,
            position_state=position_state,
            proposed_exposure_usd=proposed_exposure_usd,
            configured_max_exposure_usd=configured_max_exposure_usd,
            configured_max_open_positions=configured_max_open_positions,
            configured_max_session_loss_usd=configured_max_session_loss_usd,
            signal_ledger=PaperSignalLedger(),
            reserved_exposure_usd=0.0,
            reserved_positions=0,
        )
    return PaperCandidatePolicyEvaluation(policy_decision=decision)
