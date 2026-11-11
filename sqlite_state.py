"""Crash-recoverable SQLite snapshot storage for the bot's JSON state."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable


SCHEMA_VERSION = 1


class StateStoreError(RuntimeError):
    """Base error for persistent state failures."""


class StateStoreCorruptError(StateStoreError):
    """Raised when SQLite reports corruption; callers must not replace state."""


class SQLiteStateStore:
    """Persist one JSON-compatible state document in a durable SQLite row.

    SQLite WAL and FULL synchronous mode provide atomic commits across process
    interruptions. A legacy JSON file is imported only when the database has no
    state row; it is never deleted or rewritten by this store.
    """

    def __init__(self, path: str | Path, *, timeout_s: float = 10.0):
        self.path = Path(path)
        self.timeout_s = timeout_s
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
                integrity = db.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    raise StateStoreCorruptError(f"SQLite integrity check failed: {integrity}")
                db.execute("""
                    CREATE TABLE IF NOT EXISTS bot_state (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        schema_version INTEGER NOT NULL,
                        payload TEXT NOT NULL,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                current_version = int(db.execute("PRAGMA user_version").fetchone()[0])
                if current_version > SCHEMA_VERSION:
                    raise StateStoreError(
                        f"State database schema {current_version} is newer than supported {SCHEMA_VERSION}"
                    )
                if current_version < SCHEMA_VERSION:
                    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except sqlite3.DatabaseError as exc:
            raise StateStoreCorruptError(f"Cannot open SQLite state database {self.path}") from exc

    def load(
        self,
        default_factory: Callable[[], dict],
        *,
        legacy_json_path: str | Path | None = None,
    ) -> dict:
        try:
            with self._connection() as db:
                row = db.execute(
                    "SELECT schema_version, payload FROM bot_state WHERE singleton = 1"
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise StateStoreCorruptError(f"Cannot read SQLite state database {self.path}") from exc

        if row:
            version, payload = row
            if version > SCHEMA_VERSION:
                raise StateStoreError(f"State schema {version} is newer than supported {SCHEMA_VERSION}")
            try:
                state = json.loads(payload)
            except (json.JSONDecodeError, TypeError) as exc:
                raise StateStoreCorruptError("Stored state payload is invalid JSON") from exc
            if not isinstance(state, dict):
                raise StateStoreCorruptError("Stored state payload must be a JSON object")
            return state

        state = None
        if legacy_json_path is not None and Path(legacy_json_path).is_file():
            try:
                with Path(legacy_json_path).open("r", encoding="utf-8") as source:
                    candidate = json.load(source)
                if isinstance(candidate, dict):
                    state = candidate
            except (OSError, json.JSONDecodeError) as exc:
                raise StateStoreError(f"Legacy JSON state could not be imported: {legacy_json_path}") from exc
        if state is None:
            state = default_factory()
        self.save(state)
        return state

    def has_committed_state(self) -> bool:
        """Check whether a database contains a state row without creating one."""
        try:
            with self._connection() as db:
                return db.execute(
                    "SELECT 1 FROM bot_state WHERE singleton = 1"
                ).fetchone() is not None
        except sqlite3.DatabaseError as exc:
            raise StateStoreCorruptError(f"Cannot inspect SQLite state database {self.path}") from exc

    def save(self, state: dict) -> None:
        if not isinstance(state, dict):
            raise StateStoreError("State must be a dictionary")
        try:
            payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise StateStoreError("State contains values that cannot be stored safely") from exc
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("""
                    INSERT INTO bot_state (singleton, schema_version, payload, updated_at)
                    VALUES (1, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(singleton) DO UPDATE SET
                        schema_version = excluded.schema_version,
                        payload = excluded.payload,
                        updated_at = CURRENT_TIMESTAMP
                """, (SCHEMA_VERSION, payload))
                db.commit()
        except sqlite3.DatabaseError as exc:
            raise StateStoreError(f"SQLite state commit failed for {self.path}") from exc

    def backup_to(self, destination: str | Path) -> Path:
        """Create a consistent SQLite backup, including committed WAL data."""
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        try:
            with self._connection() as source:
                backup = sqlite3.connect(temporary)
                try:
                    source.backup(backup)
                finally:
                    backup.close()
            os.replace(temporary, target)
            return target
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

