from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from mission18_paper_admission import SQLitePaperAdmissionStore
from paper_dashboard import build_snapshot
from sqlite_state import SQLiteStateStore


def test_dashboard_exposes_only_aggregated_reservations_and_keeps_signal_status_unknown(tmp_path):
    database = tmp_path / "paper-state.sqlite3"
    SQLiteStateStore(database).save({
        "emergency_stop": False,
        "session_losses_usd": 0.0,
        "open_positions": [],
    })
    admission = SQLitePaperAdmissionStore(database)
    assert admission.reserve_entry(
        "fixture-sensitive-signal-id",
        "fixture-sensitive-correlation-key",
        10.0,
        max_exposure_usd=100.0,
        max_open_positions=2,
        max_session_loss_usd=50.0,
    ) == (True, None)
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("UPDATE bot_state SET updated_at=updated_at WHERE singleton=1")
    writer.commit()
    sidecars = [
        database,
        database.with_name(database.name + "-wal"),
        database.with_name(database.name + "-shm"),
    ]
    before = [path.read_bytes() if path.exists() else None for path in sidecars]

    try:
        snapshot = build_snapshot(database, None, None, database)
        encoded = json.dumps(snapshot, sort_keys=True)
        after = [path.read_bytes() if path.exists() else None for path in sidecars]
        assert before == after
    finally:
        writer.close()
    assert "fixture-sensitive-signal-id" not in encoded
    assert "fixture-sensitive-correlation-key" not in encoded
    assert snapshot["signals"]["status"] == "UNKNOWN"
    assert snapshot["mission18_admission_reservations"]["status"] == "DEGRADED"
    assert snapshot["mission18_admission_reservations"]["value"] == {
        "pending": 1,
        "unknown": 0,
        "committed": 0,
        "released": 0,
    }
    assert "provenance and freshness remain UNKNOWN" in snapshot[
        "mission18_admission_reservations"
    ]["detail"]


def test_dashboard_reports_unknown_when_admission_store_is_absent_or_malformed(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    assert build_snapshot(None, None, None, missing)[
        "mission18_admission_reservations"
    ]["status"] == "UNKNOWN"

    database = tmp_path / "unrelated.sqlite3"
    database.write_bytes(b"not a sqlite database")
    snapshot = build_snapshot(None, None, None, database)

    assert snapshot["mission18_admission_reservations"]["status"] == "UNKNOWN"
    assert "read failed" in snapshot["mission18_admission_reservations"]["detail"]
