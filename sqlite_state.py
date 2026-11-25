"""Crash-recoverable SQLite snapshot storage for the bot's JSON state."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable


SCHEMA_VERSION = 1


class StateStoreError(RuntimeError):
    """Base error for persistent state failures."""


class StateStoreCorruptError(StateStoreError):
    """Raised when SQLite reports corruption; callers must not replace state."""


class StateStoreConflictError(StateStoreError):
    """Raised when a snapshot was based on an older committed revision."""


class SQLiteStateStore:
    """Persist one JSON-compatible state document in a durable SQLite row.

    SQLite WAL and FULL synchronous mode provide atomic commits across process
    interruptions. A legacy JSON file is imported only when the database has no
    state row; it is never deleted or rewritten by this store.
    """

    def __init__(self, path: str | Path, *, timeout_s: float = 10.0):
        self.path = Path(path)
        self.timeout_s = timeout_s
        self.last_loaded_revision: int | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=self.timeout_s)
        db.execute(f"PRAGMA busy_timeout = {int(self.timeout_s * 1000)}")
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA synchronous = FULL")
        return db

    @contextmanager
    def _connection(self):
        db = self._connect()
        try:
            yield db
        finally:
            db.close()

    def _initialize(self) -> None:
        try:
            with self._connection() as db:
                mode = db.execute("PRAGMA journal_mode = WAL").fetchone()
                if not mode or str(mode[0]).lower() != "wal":
                    raise StateStoreError("SQLite WAL mode could not be enabled")
                integrity = db.execute("PRAGMA integrity_check").fetchall()
                if integrity != [("ok",)]:
                    raise StateStoreCorruptError(f"SQLite integrity check failed: {integrity}")
                db.execute("""
                    CREATE TABLE IF NOT EXISTS bot_state (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        schema_version INTEGER NOT NULL,
                        payload TEXT NOT NULL,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS bot_state_revision (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        revision INTEGER NOT NULL CHECK (revision >= 0)
                    )
                """)
                db.execute(
                    "INSERT OR IGNORE INTO bot_state_revision(singleton, revision) VALUES(1, 0)"
                )
                current_version = int(db.execute("PRAGMA user_version").fetchone()[0])
                if current_version > SCHEMA_VERSION:
                    raise StateStoreError(
                        f"State database schema {current_version} is newer than supported {SCHEMA_VERSION}"
                    )
                if current_version < SCHEMA_VERSION:
                    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                db.commit()
        except sqlite3.DatabaseError as exc:
            error_type = StateStoreCorruptError if self._is_corruption_error(exc) else StateStoreError
            raise error_type(f"Cannot open SQLite state database {self.path}") from exc

    @staticmethod
    def _is_corruption_error(exc: sqlite3.DatabaseError) -> bool:
        """Separate damaged database errors from transient/storage failures.

        Recovery may replace a damaged primary database from a backup. It must
        not do that for SQLITE_BUSY, SQLITE_IOERR, permission, or disk-full
        errors, where the primary may still contain the newest committed state.
        """
        code = getattr(exc, "sqlite_errorcode", None)
        if isinstance(code, int):
            primary_code = code & 0xFF
            return primary_code in {
                sqlite3.SQLITE_CORRUPT,
                sqlite3.SQLITE_NOTADB,
                getattr(sqlite3, "SQLITE_FORMAT", -1),
            }
        message = str(exc).lower()
        return any(marker in message for marker in ("database disk image is malformed", "file is not a database"))

    def load(
        self,
        default_factory: Callable[[], dict],
        *,
        legacy_json_path: str | Path | None = None,
    ) -> dict:
        try:
            with self._connection() as db:
                # Hold the SQLite writer reservation while initializing an
                # empty store. Concurrent processes then observe the same
                # committed snapshot/revision instead of racing first writes.
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT schema_version, payload FROM bot_state WHERE singleton = 1"
                ).fetchone()
                revision_row = db.execute(
                    "SELECT revision FROM bot_state_revision WHERE singleton = 1"
                ).fetchone()
                revision = int(revision_row[0]) if revision_row else 0
                self.last_loaded_revision = revision
                if row:
                    version, payload = row
                    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                        raise StateStoreCorruptError("Stored state schema version is invalid")
                    if version > SCHEMA_VERSION:
                        raise StateStoreError(f"State schema {version} is newer than supported {SCHEMA_VERSION}")
                    if version < SCHEMA_VERSION:
                        raise StateStoreError(f"State schema {version} has no supported migration to {SCHEMA_VERSION}")
                    try:
                        state = json.loads(
                            payload,
                            parse_constant=self._reject_json_constant,
                            object_pairs_hook=self._reject_duplicate_json_keys,
                        )
                    except (json.JSONDecodeError, TypeError, ValueError) as exc:
                        raise StateStoreCorruptError("Stored state payload is invalid JSON") from exc
                    if not isinstance(state, dict):
                        raise StateStoreCorruptError("Stored state payload must be a JSON object")
                    db.commit()
                    return state

                state = None
                if legacy_json_path is not None and Path(legacy_json_path).is_file():
                    try:
                        with Path(legacy_json_path).open("r", encoding="utf-8") as source:
                            candidate = json.load(
                                source,
                                parse_constant=self._reject_json_constant,
                                object_pairs_hook=self._reject_duplicate_json_keys,
                            )
                        if not isinstance(candidate, dict):
                            raise ValueError("Legacy JSON state root must be an object")
                        state = candidate
                    except (OSError, json.JSONDecodeError, ValueError) as exc:
                        raise StateStoreError(f"Legacy JSON state could not be imported: {legacy_json_path}") from exc
                if state is None:
                    state = default_factory()
                payload = self._encode_state(state)
                db.execute(
                    "INSERT INTO bot_state(singleton, schema_version, payload, updated_at) "
                    "VALUES(1, ?, ?, CURRENT_TIMESTAMP)",
                    (SCHEMA_VERSION, payload),
                )
                revision += 1
                db.execute(
                    "UPDATE bot_state_revision SET revision=? WHERE singleton=1", (revision,)
                )
                db.commit()
                self.last_loaded_revision = revision
                return state
        except sqlite3.DatabaseError as exc:
            error_type = StateStoreCorruptError if self._is_corruption_error(exc) else StateStoreError
            raise error_type(f"Cannot read SQLite state database {self.path}") from exc

    @staticmethod
    def _reject_json_constant(value: str):
        raise ValueError(f"Non-standard JSON constant {value} is not allowed")

    @staticmethod
    def _reject_duplicate_json_keys(pairs):
        state = {}
        for key, value in pairs:
            if key in state:
                raise ValueError("Duplicate JSON object key is not allowed")
            state[key] = value
        return state

    def has_committed_state(self) -> bool:
        """Check whether a database contains a state row without creating one."""
        try:
            with self._connection() as db:
                return db.execute(
                    "SELECT 1 FROM bot_state WHERE singleton = 1"
                ).fetchone() is not None
        except sqlite3.DatabaseError as exc:
            error_type = StateStoreCorruptError if self._is_corruption_error(exc) else StateStoreError
            raise error_type(f"Cannot inspect SQLite state database {self.path}") from exc

    @staticmethod
    def _encode_state(state: dict) -> str:
        if not isinstance(state, dict):
            raise StateStoreError("State must be a dictionary")
        try:
            return json.dumps(state, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise StateStoreError("State contains values that cannot be stored safely") from exc

    def save(self, state: dict, *, expected_revision: int | None = None) -> int:
        payload = self._encode_state(state)
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                revision_row = db.execute(
                    "SELECT revision FROM bot_state_revision WHERE singleton=1"
                ).fetchone()
                current_revision = int(revision_row[0]) if revision_row else 0
                if expected_revision is not None and current_revision != expected_revision:
                    db.rollback()
                    raise StateStoreConflictError(
                        "SQLite state snapshot is stale; reload before applying the update"
                    )
                db.execute("""
                    INSERT INTO bot_state (singleton, schema_version, payload, updated_at)
                    VALUES (1, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(singleton) DO UPDATE SET
                        schema_version = excluded.schema_version,
                        payload = excluded.payload,
                        updated_at = CURRENT_TIMESTAMP
                """, (SCHEMA_VERSION, payload))
                new_revision = current_revision + 1
                db.execute(
                    "UPDATE bot_state_revision SET revision=? WHERE singleton=1",
                    (new_revision,),
                )
                db.commit()
                self.last_loaded_revision = new_revision
                return new_revision
        except StateStoreConflictError:
            raise
        except sqlite3.DatabaseError as exc:
            raise StateStoreError(f"SQLite state commit failed for {self.path}") from exc

    def current_revision(self) -> int:
        """Return the current committed document revision."""
        try:
            with self._connection() as db:
                row = db.execute(
                    "SELECT revision FROM bot_state_revision WHERE singleton=1"
                ).fetchone()
                return int(row[0]) if row else 0
        except sqlite3.DatabaseError as exc:
            raise StateStoreError(f"Cannot inspect SQLite state revision for {self.path}") from exc

    def backup_to(self, destination: str | Path) -> Path:
        """Create a consistent SQLite backup, including committed WAL data."""
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_name = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, delete=False
            ) as temporary_file:
                temporary_name = temporary_file.name
            with self._connection() as source:
                backup = sqlite3.connect(temporary_name)
                try:
                    source.backup(backup)
                finally:
                    backup.close()
            os.replace(temporary_name, target)
            return target
        except Exception:
            if temporary_name is not None:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass
            raise

