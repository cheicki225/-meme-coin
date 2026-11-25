"""SQLite-backed, process-safe identity reservations for Mission 13 PAPER admission."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Sequence

from mission13_admission import PaperSignalLedger, _valid_key


class SQLitePaperSignalLedger(PaperSignalLedger):
    """Persist signal/correlation reservations across threads, processes, and restarts.

    This ledger makes identity reservations durable. It does not replace the
    caller's authoritative position state or prove that a PAPER position was
    created; callers still commit only after recording that decision.
    """

    def __init__(
        self,
        path: str | Path,
        committed: Sequence[tuple[str, str]] = (),
        *,
        timeout_s: float = 10.0,
    ) -> None:
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or timeout_s <= 0:
            raise ValueError("invalid_ledger_timeout")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout_s = float(timeout_s)
        self._initialize(committed)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=self.timeout_s)
        db.execute(f"PRAGMA busy_timeout = {int(self.timeout_s * 1000)}")
        db.execute("PRAGMA synchronous = FULL")
        return db

    @contextmanager
    def _connection(self):
        """Close each SQLite connection after its transaction completes."""
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def _initialize(self, committed: Sequence[tuple[str, str]]) -> None:
        if not isinstance(committed, Sequence) or isinstance(committed, (str, bytes)):
            raise ValueError("invalid_committed_signal_records")
        seen_signals: set[str] = set()
        seen_groups: set[str] = set()
        for item in committed:
            if (
                not isinstance(item, tuple) or len(item) != 2
                or not _valid_key(item[0]) or not _valid_key(item[1])
            ):
                raise ValueError("invalid_committed_signal_record")
            signal_id, group = item[0].strip(), item[1].strip()
            if signal_id in seen_signals or group in seen_groups:
                raise ValueError("duplicate_or_correlated_committed_signal")
            seen_signals.add(signal_id)
            seen_groups.add(group)

        try:
            with self._connection() as db:
                mode = db.execute("PRAGMA journal_mode = WAL").fetchone()
                if not mode or str(mode[0]).lower() != "wal":
                    raise RuntimeError("paper_signal_ledger_wal_unavailable")
                db.execute("BEGIN IMMEDIATE")
                db.execute("""
                    CREATE TABLE IF NOT EXISTS paper_signal_ledger (
                        signal_id TEXT PRIMARY KEY,
                        correlation_key TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL CHECK(status IN ('pending', 'committed'))
                    )
                """)
                for signal_id, group in committed:
                    normalized_signal = signal_id.strip()
                    normalized_group = group.strip()
                    existing_signal = db.execute(
                        "SELECT correlation_key FROM paper_signal_ledger WHERE signal_id=?",
                        (normalized_signal,),
                    ).fetchone()
                    if existing_signal is not None:
                        if str(existing_signal[0]) != normalized_group:
                            raise RuntimeError("paper_signal_ledger_seed_conflict")
                        continue
                    existing_group = db.execute(
                        "SELECT signal_id FROM paper_signal_ledger WHERE correlation_key=?",
                        (normalized_group,),
                    ).fetchone()
                    if existing_group is not None:
                        raise RuntimeError("paper_signal_ledger_seed_conflict")
                    db.execute(
                        "INSERT INTO paper_signal_ledger "
                        "(signal_id, correlation_key, status) VALUES(?, ?, 'committed')",
                        (normalized_signal, normalized_group),
                    )
                db.commit()
        except sqlite3.Error as exc:
            raise RuntimeError("paper_signal_ledger_initialization_failed") from exc

    def reserve(self, signal_id: str, correlation_key: str) -> tuple[bool, str | None]:
        """Atomically reserve an identity pair or fail closed on storage errors."""
        if not _valid_key(signal_id) or not _valid_key(correlation_key):
            return False, "invalid_signal_identity"
        signal_id, correlation_key = signal_id.strip(), correlation_key.strip()
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute(
                    "SELECT signal_id FROM paper_signal_ledger WHERE signal_id=?",
                    (signal_id,),
                ).fetchone()
                if existing:
                    db.commit()
                    return False, "duplicate_signal"
                existing = db.execute(
                    "SELECT 1 FROM paper_signal_ledger WHERE correlation_key=?",
                    (correlation_key,),
                ).fetchone()
                if existing:
                    db.commit()
                    return False, "correlated_signal"
                db.execute(
                    "INSERT INTO paper_signal_ledger "
                    "(signal_id, correlation_key, status) VALUES(?, ?, 'pending')",
                    (signal_id, correlation_key),
                )
                db.commit()
                return True, None
        except sqlite3.IntegrityError:
            # A concurrent writer may have reserved either unique key after
            # this process checked it. Re-read to preserve a useful refusal.
            try:
                with self._connection() as db:
                    if db.execute(
                        "SELECT 1 FROM paper_signal_ledger WHERE signal_id=?", (signal_id,)
                    ).fetchone():
                        return False, "duplicate_signal"
                    if db.execute(
                        "SELECT 1 FROM paper_signal_ledger WHERE correlation_key=?",
                        (correlation_key,),
                    ).fetchone():
                        return False, "correlated_signal"
            except sqlite3.Error:
                pass
            return False, "ledger_storage_unavailable"
        except sqlite3.Error:
            return False, "ledger_storage_unavailable"

    def commit(self, signal_id: str) -> bool:
        """Mark a pending identity committed after its PAPER decision is durable."""
        if not _valid_key(signal_id):
            return False
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                cursor = db.execute(
                    "UPDATE paper_signal_ledger SET status='committed' "
                    "WHERE signal_id=? AND status='pending'",
                    (signal_id.strip(),),
                )
                db.commit()
                return cursor.rowcount == 1
        except sqlite3.Error:
            return False

    def release(self, signal_id: str) -> bool:
        """Remove a pending reservation only when no PAPER position was recorded."""
        if not _valid_key(signal_id):
            return False
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                cursor = db.execute(
                    "DELETE FROM paper_signal_ledger WHERE signal_id=? AND status='pending'",
                    (signal_id.strip(),),
                )
                db.commit()
                return cursor.rowcount == 1
        except sqlite3.Error:
            return False

    def snapshot(self) -> tuple[tuple[str, str, str], ...]:
        """Return persisted identity/status pairs without exposing extra state."""
        try:
            with self._connection() as db:
                rows = db.execute(
                    "SELECT status, signal_id, correlation_key FROM paper_signal_ledger "
                    "ORDER BY signal_id"
                ).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError("paper_signal_ledger_read_failed") from exc
        return tuple((str(status), str(signal), str(group)) for status, signal, group in rows)
