"""Focused Mission 7 regressions for durable state and uncertain transactions."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import config
from execution_recovery import inspect_signature_on_chain, reconcile_pending_transactions_readonly
from monitoring_list import DataStore
from sqlite_state import StateStoreError


def _use_temp_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point DataStore at isolated SQLite and legacy JSON paths."""
    db_path = tmp_path / "state.sqlite3"
    monkeypatch.setattr(config, "DATA_BACKEND", "sqlite", raising=False)
    monkeypatch.setattr(config, "DATA_FILE", str(tmp_path / "legacy.json"), raising=False)
    monkeypatch.setattr(config, "SQLITE_DB_FILE", str(db_path), raising=False)
    return db_path


def test_uncommitted_sqlite_state_is_discarded_after_process_crash(tmp_path):
    """An abrupt exit before COMMIT must preserve the last durable snapshot."""
    db_path = tmp_path / "state.sqlite3"
    from sqlite_state import SQLiteStateStore

    previous = {
        "emergency_stop": True,
        "pending_transactions": [{"signature": "still-uncertain", "status": "submitted"}],
    }
    SQLiteStateStore(db_path).save(previous)

    child_code = r"""
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
connection.execute("BEGIN IMMEDIATE")
connection.execute(
    "UPDATE bot_state SET payload = ? WHERE singleton = 1",
    ('{"emergency_stop":false,"pending_transactions":[]}',),
)
os._exit(74)
"""
    child_env = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP")
        if key in os.environ
    }
    child = subprocess.run(
        [sys.executable, "-c", child_code, str(db_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=child_env,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert child.returncode == 74
    assert SQLiteStateStore(db_path).load(dict) == previous


def test_failed_sqlite_resolution_preserves_uncertain_transaction_after_restart(
    tmp_path, monkeypatch
):
    """A failed durable resolution cannot drop a pending signature or its fee history."""
    db_path = _use_temp_sqlite(tmp_path, monkeypatch)
    store = DataStore()
    store.state["pending_transactions"] = [
        {"signature": "finalized-failure", "status": "submitted"}
    ]
    store.state["resolved_transactions"] = []
    store.state["unapplied_failed_transaction_fees_lamports"] = 200
    store.save()

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_state_update BEFORE UPDATE ON bot_state
            BEGIN SELECT RAISE(ABORT, 'simulated SQLite storage fault'); END
            """
        )

    with pytest.raises(StateStoreError, match="commit failed"):
        store.resolve_pending_transaction(
            "finalized-failure",
            {"chain_status": "finalized_failed", "fee_lamports": 5000},
        )

    assert store.state["pending_transactions"] == [
        {"signature": "finalized-failure", "status": "submitted"}
    ]
    assert store.state["resolved_transactions"] == []
    assert store.state["unapplied_failed_transaction_fees_lamports"] == 200

    restarted = DataStore()
    assert restarted.state["pending_transactions"] == [
        {"signature": "finalized-failure", "status": "submitted"}
    ]
    assert restarted.state["resolved_transactions"] == []
    assert restarted.state["unapplied_failed_transaction_fees_lamports"] == 200


def test_unknown_submission_stays_durable_after_readonly_reconciliation(
    tmp_path, monkeypatch
):
    _use_temp_sqlite(tmp_path, monkeypatch)
    signature = "signature-after-ambiguous-send"
    store = DataStore()
    store.record_pending_transaction({
        "signature": signature,
        "kind": "manual_sol_transfer",
        "status": "submitted",
    })
    assert store.mark_pending_transaction_unknown(signature) is True

    reloaded = DataStore()
    methods = []

    async def read_only_rpc(payload, timeout):
        methods.append(payload["method"])
        assert payload["params"][0] == [signature]
        return {"value": [None]}

    async def inspect(pending_signature):
        return await inspect_signature_on_chain(
            pending_signature, rpc_post=read_only_rpc
        )

    result = asyncio.run(reconcile_pending_transactions_readonly(
        reloaded, inspector=inspect, max_checks=1
    ))

    assert methods == ["getSignatureStatuses"]
    assert all(method != "sendTransaction" for method in methods)
    assert result["pending_or_unknown"] == 1
    assert result["remaining"] == 1
    assert reloaded.state["pending_transactions"] == [{
        "signature": signature,
        "kind": "manual_sol_transfer",
        "status": "unknown",
    }]


def test_rpc_inspection_exception_keeps_transaction_unresolved():
    """A timeout during reconciliation must retain the pending record."""
    store = object.__new__(DataStore)
    store.state = {
        "open_positions": [],
        "closed_positions": [],
        "pending_transactions": [{"signature": "rpc-timeout", "status": "submitted"}],
    }

    async def inspection_times_out(_signature: str) -> dict:
        raise TimeoutError("simulated read-only RPC timeout")

    result = asyncio.run(
        reconcile_pending_transactions_readonly(store, inspector=inspection_times_out)
    )

    assert result["checked"] == 1
    assert result["pending_or_unknown"] == 1
    assert result["remaining"] == 1
    assert store.state["pending_transactions"] == [
        {"signature": "rpc-timeout", "status": "submitted"}
    ]
