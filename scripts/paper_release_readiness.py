"""Read-only local checks for the PAPER release candidate.

This script does not load dotenv files, inspect secret values, contact a
provider, or access a wallet. A LOCAL_CHECKS_READY result covers only the
checks reported here; it is not evidence of live data provenance or a PAPER soak.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Any


def _point(status: str, detail: str) -> dict[str, str]:
    return {"status": status, "detail": detail}


def check_runtime_flags(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Require explicit process flags; this check never infers the app config."""
    values = os.environ if environ is None else environ
    raw_mode = values.get("TRADING_MODE")
    raw_live_enabled = values.get("LIVE_EXECUTION_ENABLED")
    if raw_mode is None or raw_live_enabled is None:
        return _point(
            "UNKNOWN",
            "explicit PAPER and LIVE flags are required in this process environment",
        )

    mode = raw_mode.strip().upper()
    live_value = raw_live_enabled.strip().lower()
    if mode != "PAPER":
        return _point("BLOCKED", "TRADING_MODE must be PAPER")
    if live_value not in {"true", "false"}:
        return _point("UNKNOWN", "LIVE_EXECUTION_ENABLED is missing or malformed")
    if live_value == "true":
        return _point("BLOCKED", "LIVE_EXECUTION_ENABLED must be false")
    return _point("READY", "PAPER mode selected; LIVE execution disabled")


def check_state_database(path: str | Path) -> dict[str, str]:
    """Read SQLite integrity and emergency-stop state without opening writes."""
    database = Path(path)
    if not database.is_file():
        return _point("UNKNOWN", "state database is not present")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only = ON")
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            return _point("BLOCKED", "SQLite integrity check failed")
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bot_state'"
        ).fetchone()
        if not table:
            return _point("UNKNOWN", "bot_state table is not present")
        row = connection.execute(
            "SELECT payload FROM bot_state WHERE singleton=1"
        ).fetchone()
        if not row or not isinstance(row[0], str):
            return _point("UNKNOWN", "persisted bot state is not present")
        state: Any = json.loads(row[0])
        if not isinstance(state, dict):
            return _point("UNKNOWN", "persisted bot state is malformed")
        if state.get("emergency_stop") is False:
            return _point("BLOCKED", "emergency stop is not active")
        if state.get("emergency_stop") is not True:
            return _point("UNKNOWN", "emergency-stop state is missing or malformed")
        if "open_positions" not in state or "pending_transactions" not in state:
            return _point("UNKNOWN", "required position or pending-transaction state is missing")

        positions = state["open_positions"]
        pending_transactions = state["pending_transactions"]
        if not isinstance(positions, list) or any(
            not isinstance(position, dict) for position in positions
        ):
            return _point("UNKNOWN", "open-position state is malformed")
        if not isinstance(pending_transactions, list) or any(
            not isinstance(item, dict) for item in pending_transactions
        ):
            return _point("UNKNOWN", "pending-transaction state is malformed")
        if pending_transactions:
            return _point("BLOCKED", "pending transaction signatures require reconciliation")
        if any(position.get("exit_status") == "unknown" for position in positions):
            return _point("BLOCKED", "one or more open positions have UNKNOWN exits")
        if positions:
            return _point(
                "UNKNOWN",
                "open positions exist; runtime monitor heartbeat is not verifiable offline",
            )
        return _point("READY", "SQLite is healthy; emergency stop is active; no open or pending positions")
    except (OSError, sqlite3.Error, json.JSONDecodeError, ValueError):
        return _point("UNKNOWN", "state database could not be read safely")
    finally:
        if connection is not None:
            connection.close()


def check_helius_database(path: str | Path) -> dict[str, str]:
    """Report unresolved durable inbox state without resolving or replaying it."""
    database = Path(path)
    if not database.is_file():
        return _point("UNKNOWN", "Helius event database is not present")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only = ON")
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            return _point("BLOCKED", "SQLite integrity check failed")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "event_inbox" not in tables or "event_streams" not in tables:
            return _point("UNKNOWN", "Helius inbox schema is unavailable")
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if schema_version < 4:
            return _point("UNKNOWN", "Helius inbox schema v4 is required for canonical order checks")
        inbox_info = {
            row[1]: row for row in connection.execute("PRAGMA table_info(event_inbox)")
        }
        stream_info = {
            row[1]: row for row in connection.execute("PRAGMA table_info(event_streams)")
        }
        inbox_columns = set(inbox_info)
        stream_columns = set(stream_info)
        if not {"slot", "transaction_index", "status"}.issubset(inbox_columns):
            return _point("UNKNOWN", "Helius inbox lacks canonical-order fields")
        if not {"recovery_required", "intake_paused"}.issubset(stream_columns):
            return _point("UNKNOWN", "Helius recovery state is unavailable")
        if any(
            (inbox_info[column][2] or "").upper() != "INTEGER"
            for column in ("slot", "transaction_index")
        ) or (inbox_info["status"][2] or "").upper() != "TEXT":
            return _point("UNKNOWN", "Helius inbox column types are malformed")
        if any(
            (stream_info[column][2] or "").upper() != "INTEGER"
            or stream_info[column][3] != 1
            for column in ("recovery_required", "intake_paused")
        ):
            return _point("UNKNOWN", "Helius recovery column types are malformed")
        unknown = connection.execute(
            "SELECT COUNT(*) FROM event_inbox WHERE status='unknown'"
        ).fetchone()[0]
        paused = connection.execute(
            "SELECT COUNT(*) FROM event_streams WHERE intake_paused=1"
        ).fetchone()[0]
        recovery_required = connection.execute(
            "SELECT COUNT(*) FROM event_streams WHERE recovery_required=1"
        ).fetchone()[0]
        malformed_flags = connection.execute(
            "SELECT COUNT(*) FROM event_streams WHERE "
            "typeof(recovery_required)!='integer' OR recovery_required NOT IN (0,1) "
            "OR typeof(intake_paused)!='integer' OR intake_paused NOT IN (0,1)"
        ).fetchone()[0]
        missing_order = connection.execute(
            "SELECT COUNT(*) FROM event_inbox WHERE slot IS NULL OR transaction_index IS NULL"
        ).fetchone()[0]
        malformed_order = connection.execute(
            "SELECT COUNT(*) FROM event_inbox WHERE "
            "(slot IS NOT NULL AND (typeof(slot)!='integer' OR slot<0)) "
            "OR (transaction_index IS NOT NULL AND "
            "(typeof(transaction_index)!='integer' OR transaction_index<0))"
        ).fetchone()[0]
        malformed_status = connection.execute(
            "SELECT COUNT(*) FROM event_inbox WHERE status IS NULL OR status NOT IN "
            "('pending','retry','processing','processed','unknown')"
        ).fetchone()[0]
        if int(malformed_flags) or int(malformed_order) or int(malformed_status):
            return _point("BLOCKED", "Helius order, status, or recovery flags are malformed")
        if int(unknown) or int(paused) or int(recovery_required):
            return _point("BLOCKED", "one or more Helius streams are UNKNOWN or paused")
        if int(missing_order):
            return _point("BLOCKED", "one or more Helius events lack canonical slot/index")
        pending = connection.execute(
            "SELECT COUNT(*) FROM event_inbox WHERE status IN ('pending','retry','processing')"
        ).fetchone()[0]
        if int(pending):
            return _point("UNKNOWN", "Helius inbox has unresolved pending work")
        total = connection.execute("SELECT COUNT(*) FROM event_inbox").fetchone()[0]
        if not int(total):
            return _point("UNKNOWN", "no Helius events are recorded")
        return _point("READY", "inbox integrity is valid; no unresolved events")
    except (OSError, sqlite3.Error, ValueError):
        return _point("UNKNOWN", "Helius event database could not be read safely")
    finally:
        if connection is not None:
            connection.close()


def build_report(
    state_path: str | Path,
    helius_path: str | Path,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Collect offline checks; explicit flags must be passed by this process."""
    checks = {
        "runtime_flags": check_runtime_flags(environ),
        "state_database": check_state_database(state_path),
        "helius_database": check_helius_database(helius_path),
    }
    statuses = {item["status"] for item in checks.values()}
    overall = (
        "BLOCKED" if "BLOCKED" in statuses
        else "MANUAL_VERIFICATION_REQUIRED" if "UNKNOWN" in statuses
        else "LOCAL_CHECKS_READY"
    )
    return {
        "status": overall,
        "scope": "local_read_only_checks_only",
        "checks": checks,
        "network_access": False,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only local PAPER release-candidate readiness checks."
    )
    parser.add_argument("--state-db", default="sniper_data.sqlite3")
    parser.add_argument("--helius-db", default="data/helius_events.sqlite3")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = build_report(args.state_db, args.helius_db)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["status"] == "LOCAL_CHECKS_READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
