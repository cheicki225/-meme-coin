from __future__ import annotations

import http.client
import json
import sqlite3
import threading
import time
from pathlib import Path

import paper_dashboard


def _write_state_db(path: Path, state: dict) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE bot_state (singleton INTEGER PRIMARY KEY, schema_version INTEGER, "
            "payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO bot_state VALUES (1, 1, ?, '2026-11-24 00:00:00')",
            (json.dumps(state, allow_nan=False),),
        )


def _write_helius_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE event_streams (
                stream TEXT PRIMARY KEY,
                checkpoint_sequence INTEGER NOT NULL,
                checkpoint_signature TEXT,
                recovery_required INTEGER NOT NULL,
                intake_paused INTEGER NOT NULL
            );
            CREATE TABLE event_inbox (
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                received_at REAL NOT NULL,
                processed_at REAL
            );
            CREATE TABLE event_counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            """
        )
        connection.execute(
            "INSERT INTO event_streams VALUES (?, 8, ?, 0, 0)",
            ("wallet-secret-address", "signature-secret-value"),
        )
        connection.execute("INSERT INTO event_streams VALUES ('second-stream', 0, NULL, 1, 1)")
        connection.executemany(
            "INSERT INTO event_inbox VALUES (?, ?, ?, ?)",
            [
                ("pending", '{"token_mint":"mint-secret-value"}', 1795478400, None),
                ("processed", "", 1795478300, 1795478350),
            ],
        )
        connection.executemany(
            "INSERT INTO event_counters VALUES (?, ?)",
            [("received", 4), ("duplicates", 2), ("replayed", 1), ("retried", 3), ("queue_full", 1)],
        )


def test_missing_sources_are_explicitly_unknown() -> None:
    snapshot = paper_dashboard.build_snapshot(None, None, None)

    assert snapshot["runtime_mode"]["status"] == "UNKNOWN"
    assert snapshot["runtime_heartbeat"]["status"] == "UNKNOWN"
    assert snapshot["rpc_health"]["status"] == "UNKNOWN"
    assert snapshot["helius_provider_health"]["status"] == "UNKNOWN"
    assert snapshot["signals"]["status"] == "UNKNOWN"
    assert snapshot["telegram_delivery"]["status"] == "UNKNOWN"
    assert snapshot["open_positions"]["status"] == "UNKNOWN"
    assert snapshot["pnl_recorded_usd"]["status"] == "UNKNOWN"
    assert snapshot["generated_at_utc"].endswith("+00:00")


def test_state_snapshot_is_read_only_aggregated_and_redacted(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    secret = "private-address-or-secret-marker"
    state = {
        "emergency_stop": True,
        "emergency_stop_reason": secret,
        "open_positions": [{
            "token_mint": secret,
            "source_wallet": secret,
            "cost_basis_usd": 12.5,
            "fee_accounting_version": 1,
            "simulated_fees_usd": 0.4,
            "fee_accounting_complete": True,
        }],
        "closed_positions": [{
            "signature": secret,
            "fee_accounting_version": 1,
            "simulated_fees_usd": 0.6,
            "fee_accounting_complete": True,
        }],
        "trades": [],
        "pnl_fee_accounting_version": 1,
        "pnl_fee_history_incomplete": False,
        "total_pnl_usd": 7.25,
        "wallet_private_key": secret,
        "telegram_bot_token": secret,
    }
    _write_state_db(database, state)

    before = database.read_bytes()
    snapshot = paper_dashboard.build_snapshot(database, None, None)
    after = database.read_bytes()
    encoded = json.dumps(snapshot, sort_keys=True)

    assert before == after
    assert secret not in encoded
    assert snapshot["emergency_stop"]["value"] is True
    assert snapshot["emergency_stop"]["status"] == "OBSERVED"
    assert snapshot["open_positions"]["value"] == 1
    assert snapshot["exposure_cost_basis_usd"]["value"] == 12.5
    assert snapshot["pnl_recorded_usd"]["value"] == 7.25
    assert snapshot["fee_history"]["status"] == "OBSERVED"
    assert snapshot["fee_history"]["value"] == {"recorded_usd": 1.0, "history_complete": True}
    assert snapshot["unrealized_pnl_usd"]["status"] == "UNKNOWN"
    assert snapshot["emergency_stop"]["source"]
    assert snapshot["emergency_stop"]["as_of"]


def test_wal_state_is_read_without_changing_database_or_sidecars(tmp_path: Path) -> None:
    database = tmp_path / "wal-state.sqlite3"
    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "CREATE TABLE bot_state (singleton INTEGER PRIMARY KEY, schema_version INTEGER, "
            "payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        writer.execute(
            "INSERT INTO bot_state VALUES (1, 1, ?, '2026-11-24 00:00:00')",
            (json.dumps({"emergency_stop": True, "open_positions": [], "closed_positions": [], "trades": []}),),
        )
        writer.commit()
        sidecars = [database, database.with_name(database.name + "-wal"), database.with_name(database.name + "-shm")]
        before = [path.stat().st_size if path.exists() else None for path in sidecars]

        snapshot = paper_dashboard.build_snapshot(database, None, None)

        after = [path.stat().st_size if path.exists() else None for path in sidecars]
        assert snapshot["emergency_stop"]["value"] is True
        assert before == after
    finally:
        writer.close()


def test_helius_snapshot_exposes_aggregate_counts_only(tmp_path: Path) -> None:
    database = tmp_path / "events.sqlite3"
    _write_helius_db(database)

    snapshot = paper_dashboard.build_snapshot(None, database, None)
    encoded = json.dumps(snapshot, sort_keys=True)

    assert snapshot["helius_inbox"]["status"] == "OBSERVED"
    assert snapshot["helius_inbox"]["value"]["pending"] == 1
    assert snapshot["helius_inbox"]["value"]["processed"] == 1
    assert snapshot["helius_inbox"]["value"]["received"] == 4
    assert snapshot["helius_inbox"]["value"]["queue_full"] == 1
    assert snapshot["helius_event_freshness"]["value"]["latest_received_at_utc"] == "2026-11-24T00:00:00+00:00"
    assert snapshot["helius_checkpoints"]["status"] == "DEGRADED"
    assert snapshot["helius_checkpoints"]["value"]["checkpointed_streams"] == 1
    assert snapshot["helius_checkpoints"]["value"]["recovery_required_streams"] == 1
    for hidden in ("wallet-secret-address", "signature-secret-value", "mint-secret-value"):
        assert hidden not in encoded


def test_metrics_reader_filters_unapproved_labels_and_private_fields(tmp_path: Path) -> None:
    metrics = tmp_path / "metrics.jsonl"
    private_marker = "correlation-secret-marker"
    metrics.write_text(
        "\n".join([
            json.dumps({
                "ts": 1795478400,
                "stage": "rpc.request",
                "provider": "solana_rpc",
                "outcome": "success",
                "duration_ms": 10,
                "correlation_id": private_marker,
                "wallet": private_marker,
            }),
            json.dumps({
                "ts": 1795478401,
                "stage": "rpc.request",
                "provider": "solana_rpc",
                "outcome": "failure",
                "duration_ms": 30,
                "error": private_marker,
            }),
            json.dumps({
                "ts": 1795478402,
                "stage": private_marker,
                "provider": "solana_rpc",
                "outcome": "success",
                "duration_ms": 1,
            }),
            json.dumps({
                "stage": "rpc.request",
                "provider": "solana_rpc",
                "outcome": "success",
                "duration_ms": 1,
            }),
            "{malformed",
        ]) + "\n",
        encoding="utf-8",
    )

    snapshot = paper_dashboard.build_snapshot(None, None, metrics)
    performance = snapshot["performance"]["value"]
    encoded = json.dumps(snapshot, sort_keys=True)

    assert snapshot["performance"]["status"] == "INCOMPLETE"
    assert performance["events"] == 2
    assert performance["invalid_lines_omitted"] == 3
    assert performance["observed_failure_or_unknown_events"] == 1
    assert performance["dropped_event_count"] is None
    assert performance["stages"] == [{
        "stage": "rpc.request",
        "provider": "solana_rpc",
        "count": 2,
        "failure_rate": 0.5,
        "p50_ms": 20.0,
        "p95_ms": 29.0,
        "p99_ms": 29.8,
    }]
    assert snapshot["observed_errors"]["value"] == 1
    assert private_marker not in encoded


def test_malformed_sources_and_fee_history_remain_unknown_or_incomplete(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE bot_state (singleton INTEGER, payload TEXT)")
        connection.execute("INSERT INTO bot_state VALUES (1, '{bad json')")
    metrics = tmp_path / "metrics.jsonl"
    metrics.write_text("{bad json\n", encoding="utf-8")

    snapshot = paper_dashboard.build_snapshot(database, tmp_path / "missing.db", metrics)

    assert snapshot["emergency_stop"]["status"] == "UNKNOWN"
    assert snapshot["open_positions"]["status"] == "UNKNOWN"
    assert snapshot["helius_inbox"]["status"] == "UNKNOWN"
    assert snapshot["performance"]["status"] == "INCOMPLETE"
    assert snapshot["performance"]["value"]["events"] == 0
    assert snapshot["performance"]["value"]["invalid_lines_omitted"] == 1
    assert snapshot["observed_errors"]["value"] == 0


def test_fee_and_pnl_completeness_require_matching_accounting_versions(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    _write_state_db(database, {
        "emergency_stop": True,
        "open_positions": [{
            "cost_basis_usd": 4.0,
            "simulated_fees_usd": 0.2,
            "fee_accounting_version": 99,
            "fee_accounting_complete": True,
        }],
        "closed_positions": [],
        "trades": [],
        "total_pnl_usd": 1.5,
        "pnl_fee_accounting_version": 1,
        "pnl_fee_history_incomplete": False,
    })

    snapshot = paper_dashboard.build_snapshot(database, None, None)

    assert snapshot["pnl_recorded_usd"]["status"] == "INCOMPLETE"
    assert snapshot["fee_history"]["status"] == "INCOMPLETE"
    assert snapshot["fee_history"]["value"] == {"recorded_usd": 0.2, "history_complete": False}


def test_aggregate_pnl_is_incomplete_without_matching_version(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    _write_state_db(database, {
        "emergency_stop": True,
        "open_positions": [],
        "closed_positions": [],
        "trades": [],
        "total_pnl_usd": 1.5,
        "pnl_fee_history_incomplete": False,
    })

    snapshot = paper_dashboard.build_snapshot(database, None, None)

    assert snapshot["pnl_recorded_usd"]["status"] == "INCOMPLETE"
    assert snapshot["fee_history"]["status"] == "INCOMPLETE"


def test_trade_fee_history_requires_matching_version(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    _write_state_db(database, {
        "emergency_stop": True,
        "open_positions": [],
        "closed_positions": [],
        "trades": [{
            "pnl_usd": 1.0,
            "fees_usd": 0.1,
            "fee_accounting_version": 2,
            "fee_accounting_complete": True,
        }],
        "total_pnl_usd": 1.0,
        "pnl_fee_accounting_version": 1,
        "pnl_fee_history_incomplete": False,
    })

    snapshot = paper_dashboard.build_snapshot(database, None, None)

    assert snapshot["pnl_recorded_usd"]["status"] == "INCOMPLETE"
    assert snapshot["fee_history"]["status"] == "INCOMPLETE"


def test_trade_fee_rows_without_position_records_are_not_reported_as_zero(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    _write_state_db(database, {
        "emergency_stop": True,
        "open_positions": [],
        "closed_positions": [],
        "trades": [{
            "pnl_usd": 1.0,
            "fees_usd": 0.1,
            "fee_accounting_version": 1,
            "fee_accounting_complete": True,
        }],
        "total_pnl_usd": 1.0,
        "pnl_fee_accounting_version": 1,
        "pnl_fee_history_incomplete": False,
    })

    snapshot = paper_dashboard.build_snapshot(database, None, None)

    assert snapshot["pnl_recorded_usd"]["status"] == "INCOMPLETE"
    assert snapshot["fee_history"]["status"] == "INCOMPLETE"
    assert snapshot["fee_history"]["value"] == {"recorded_usd": None, "history_complete": False}


def test_snapshot_reads_latest_state_after_restart_without_caching(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    _write_state_db(database, {
        "emergency_stop": True,
        "open_positions": [],
        "closed_positions": [],
        "trades": [],
        "total_pnl_usd": 0,
        "pnl_fee_history_incomplete": False,
        "pnl_fee_accounting_version": 1,
    })
    first = paper_dashboard.build_snapshot(database, None, None)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE bot_state SET payload=? WHERE singleton=1",
            (json.dumps({
                "emergency_stop": False,
                "open_positions": [{"cost_basis_usd": 9.0}],
                "closed_positions": [],
                "trades": [],
                "total_pnl_usd": 2.0,
                "pnl_fee_history_incomplete": True,
            }),),
        )
    second = paper_dashboard.build_snapshot(database, None, None)

    assert first["emergency_stop"]["value"] is True
    assert second["emergency_stop"]["value"] is False
    assert second["open_positions"]["value"] == 1
    assert second["exposure_cost_basis_usd"]["value"] == 9.0
    assert second["fee_history"]["status"] == "INCOMPLETE"


def test_state_read_timeout_fails_to_unknown_without_blocking(tmp_path: Path) -> None:
    database = tmp_path / "locked.sqlite3"
    _write_state_db(database, {"emergency_stop": True})
    lock = sqlite3.connect(database, timeout=0.1)
    lock.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    try:
        snapshot = paper_dashboard.build_snapshot(database, None, None)
    finally:
        lock.rollback()
        lock.close()

    elapsed = time.monotonic() - started
    assert snapshot["emergency_stop"]["status"] == "UNKNOWN"
    assert elapsed < 1.0


def test_truncated_metrics_file_is_marked_incomplete(tmp_path: Path) -> None:
    metrics = tmp_path / "large-metrics.jsonl"
    metrics.write_bytes(b"x" * (paper_dashboard._MAX_METRICS_FILE_BYTES + 100))

    snapshot = paper_dashboard.build_snapshot(None, None, metrics)

    assert snapshot["performance"]["status"] == "INCOMPLETE"
    assert snapshot["performance"]["value"]["events"] == 0
    assert snapshot["performance"]["value"]["truncated_files"] == 1


def test_http_server_is_loopback_only_and_rejects_writes() -> None:
    server = paper_dashboard.create_server(None, None, None, port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        assert server.server_address[0] == "127.0.0.1"
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", "/api/status")
        response = connection.getresponse()
        snapshot = json.loads(response.read())
        assert response.status == 200
        assert snapshot["rpc_health"]["status"] == "UNKNOWN"
        assert response.getheader("Cache-Control") == "no-store"

        connection.request("POST", "/api/status", body=b"{}")
        response = connection.getresponse()
        assert response.status == 405
        assert response.read() == b"Method not allowed"
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
