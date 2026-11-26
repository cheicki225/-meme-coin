from __future__ import annotations

import json

from mission18_paper_admission import SQLitePaperAdmissionStore
from paper_dashboard import build_snapshot
from sqlite_state import SQLiteStateStore


def test_unknown_reservation_survives_reopen_and_is_visible_as_degraded(tmp_path):
    database = tmp_path / "paper-state.sqlite3"
    SQLiteStateStore(database).save({
        "emergency_stop": False,
        "session_losses_usd": 0.0,
        "open_positions": [],
        "closed_positions": [],
        "total_pnl_usd": 0.0,
    })
    admission = SQLitePaperAdmissionStore(database)
    signal_id = "fixture-sensitive-signal"
    correlation_key = "fixture-sensitive-correlation"
    assert admission.reserve_entry(
        signal_id,
        correlation_key,
        10.0,
        max_exposure_usd=100.0,
        max_open_positions=2,
        max_session_loss_usd=50.0,
    ) == (True, None)
    assert admission.mark_unknown(signal_id) is True

    restarted = SQLitePaperAdmissionStore(database)
    counts = restarted.counts()
    snapshot = build_snapshot(database, None, None, database)
    encoded = json.dumps(snapshot, sort_keys=True)

    assert counts["unknown"] == 1
    assert restarted.snapshot().reserved_positions == 1
    assert snapshot["emergency_stop"]["value"] is False
    assert snapshot["mission18_admission_reservations"]["status"] == "DEGRADED"
    assert snapshot["mission18_admission_reservations"]["value"] == {
        "pending": 0,
        "unknown": 1,
        "committed": 0,
        "released": 0,
    }
    assert snapshot["signals"]["status"] == "UNKNOWN"
    assert signal_id not in encoded
    assert correlation_key not in encoded
