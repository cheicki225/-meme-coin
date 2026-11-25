import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from helius_event_store import HeliusEventStore, process_next_event
from scripts import paper_release_readiness


def _state_database(
    path, *, emergency_stop=True, open_positions=None, pending_transactions=None
):
    payload = {
        "emergency_stop": emergency_stop,
        "open_positions": [] if open_positions is None else open_positions,
        "pending_transactions": (
            [] if pending_transactions is None else pending_transactions
        ),
    }
    with sqlite3.connect(path) as database:
        database.execute(
            "CREATE TABLE bot_state (singleton INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )
        database.execute(
            "INSERT INTO bot_state(singleton, payload) VALUES(1, ?)",
            (json.dumps(payload),),
        )


def test_runtime_flag_check_requires_paper_and_live_disabled():
    assert paper_release_readiness.check_runtime_flags({})["status"] == "UNKNOWN"
    assert paper_release_readiness.check_runtime_flags(
        {"TRADING_MODE": "PAPER", "LIVE_EXECUTION_ENABLED": "false"}
    )["status"] == "READY"
    assert paper_release_readiness.check_runtime_flags(
        {"TRADING_MODE": "LIVE", "LIVE_EXECUTION_ENABLED": "false"}
    )["status"] == "BLOCKED"


def test_paper_example_configuration_contains_no_credentials():
    example = Path(__file__).resolve().parents[1] / ".env.paper.example"
    values = {
        line.partition("=")[0]: line.partition("=")[2]
        for line in example.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }

    assert values["TRADING_MODE"] == "PAPER"
    assert values["LIVE_EXECUTION_ENABLED"].lower() == "false"
    assert all(
        values[key] == ""
        for key in (
            "HELIUS_API_KEY", "SOLANA_PRIVATE_KEY", "WALLET_ENCRYPTION_KEY",
            "JUPITER_API_KEY", "TELEGRAM_BOT_TOKEN", "BOT_TOKEN",
        )
    )
    assert paper_release_readiness.check_runtime_flags(
        {"TRADING_MODE": "PAPER", "LIVE_EXECUTION_ENABLED": "true"}
    )["status"] == "BLOCKED"
    assert paper_release_readiness.check_runtime_flags(
        {"TRADING_MODE": "PAPER", "LIVE_EXECUTION_ENABLED": "unset"}
    )["status"] == "UNKNOWN"


def test_missing_databases_require_manual_verification(tmp_path):
    report = paper_release_readiness.build_report(
        tmp_path / "missing-state.sqlite3",
        tmp_path / "missing-helius.sqlite3",
        {},
    )

    assert report["status"] == "MANUAL_VERIFICATION_REQUIRED"
    assert report["network_access"] is False
    assert report["checks"]["state_database"]["status"] == "UNKNOWN"


def test_state_database_readiness_blocks_when_emergency_stop_is_inactive(tmp_path):
    state_path = tmp_path / "state.sqlite3"
    _state_database(state_path, emergency_stop=False)

    result = paper_release_readiness.check_state_database(state_path)

    assert result["status"] == "BLOCKED"
    assert "not active" in result["detail"]


def test_state_database_blocks_pending_transactions(tmp_path):
    state_path = tmp_path / "pending-state.sqlite3"
    _state_database(
        state_path,
        pending_transactions=[{"signature": "synthetic-signature", "status": "unknown"}],
    )

    result = paper_release_readiness.check_state_database(state_path)

    assert result["status"] == "BLOCKED"
    assert "require reconciliation" in result["detail"]


def test_state_database_requires_position_and_pending_collections(tmp_path):
    state_path = tmp_path / "incomplete-state.sqlite3"
    with sqlite3.connect(state_path) as database:
        database.execute(
            "CREATE TABLE bot_state (singleton INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )
        database.execute(
            "INSERT INTO bot_state(singleton, payload) VALUES(1, ?)",
            (json.dumps({"emergency_stop": True}),),
        )

    result = paper_release_readiness.check_state_database(state_path)

    assert result["status"] == "UNKNOWN"


def test_helius_readiness_blocks_processed_rows_without_migrated_canonical_index(
    tmp_path,
):
    event_path = tmp_path / "legacy-events.sqlite3"
    store = HeliusEventStore(event_path)
    stream = "copytrade:wallet-public"
    store.enqueue(
        stream, "legacy-processed", {}, slot=123,
        transaction_index=4, is_replay=True,
    )
    event = store.claim_next(stream)
    assert event is not None
    store.mark_processed(event.event_id, claim_generation=event.claim_generation)
    with sqlite3.connect(event_path) as database:
        database.execute(
            "UPDATE event_inbox SET transaction_index=NULL WHERE signature=?",
            ("legacy-processed",),
        )

    result = paper_release_readiness.check_helius_database(event_path)

    assert result["status"] == "BLOCKED"
    assert "canonical slot/index" in result["detail"]


def test_helius_readiness_blocks_recovery_required_stream(tmp_path):
    event_path = tmp_path / "recovery-events.sqlite3"
    store = HeliusEventStore(event_path)
    store.mark_recovery_required("protection:wallet-public", "synthetic_gap")

    result = paper_release_readiness.check_helius_database(event_path)

    assert result["status"] == "BLOCKED"


def test_helius_readiness_requires_schema_v4(tmp_path):
    event_path = tmp_path / "old-version-events.sqlite3"
    HeliusEventStore(event_path)
    with sqlite3.connect(event_path) as database:
        database.execute("PRAGMA user_version=3")

    result = paper_release_readiness.check_helius_database(event_path)

    assert result["status"] == "UNKNOWN"
    assert "schema v4" in result["detail"]


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("event_inbox", "slot", -1),
        ("event_inbox", "slot", 123.5),
        ("event_inbox", "transaction_index", -1),
        ("event_inbox", "transaction_index", 1.5),
        ("event_streams", "recovery_required", 2),
        ("event_streams", "intake_paused", -1),
        ("event_inbox", "status", "malformed-status"),
    ],
)
def test_helius_readiness_blocks_malformed_persisted_values(
    tmp_path, table, column, value
):
    event_path = tmp_path / "malformed-events.sqlite3"
    store = HeliusEventStore(event_path)
    stream = "new-token:program"
    store.enqueue(
        stream, "malformed-order", {}, slot=123,
        transaction_index=4, is_replay=True,
    )
    with sqlite3.connect(event_path) as database:
        if column == "status":
            database.execute("PRAGMA ignore_check_constraints=ON")
        if table == "event_inbox":
            database.execute(
                f"UPDATE {table} SET {column}=? WHERE signature=?",
                (value, "malformed-order"),
            )
        else:
            database.execute(
                f"UPDATE {table} SET {column}=? WHERE stream=?", (value, stream)
            )

    result = paper_release_readiness.check_helius_database(event_path)

    assert result["status"] == "BLOCKED"
    assert "malformed" in result["detail"]


def test_helius_unknown_is_blocked_and_never_resolved_by_readiness_check(tmp_path):
    event_path = tmp_path / "events.sqlite3"
    store = HeliusEventStore(event_path)
    stream = "new-token:program"
    store.enqueue(stream, "live-signature", {}, slot=123)

    async def handler(_event):
        raise AssertionError("unindexed live event must not reach its handler")

    assert asyncio.run(process_next_event(store, stream, handler)) == "unknown"

    result = paper_release_readiness.check_helius_database(event_path)

    assert result["status"] == "BLOCKED"
    assert store.has_unknown(stream)
    assert store.intake_paused(stream)


def test_ready_report_needs_active_stop_and_clean_recorded_helius_history(tmp_path):
    state_path = tmp_path / "state.sqlite3"
    event_path = tmp_path / "events.sqlite3"
    _state_database(state_path)
    store = HeliusEventStore(event_path)
    store.enqueue(
        "new-token:program", "history-signature", {}, slot=123,
        transaction_index=5, is_replay=True,
    )
    event = store.claim_next("new-token:program")
    assert event is not None
    store.mark_processed(event.event_id, claim_generation=event.claim_generation)

    report = paper_release_readiness.build_report(
        state_path,
        event_path,
        {"TRADING_MODE": "PAPER", "LIVE_EXECUTION_ENABLED": "false"},
    )

    assert report["status"] == "LOCAL_CHECKS_READY"
    assert report["checks"]["runtime_flags"]["status"] == "READY"
