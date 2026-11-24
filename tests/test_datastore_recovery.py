import json
import os
import subprocess
import sqlite3
import sys
from pathlib import Path

import pytest

import config
from monitoring_list import DataStore
from sqlite_state import StateStoreCorruptError, StateStoreError


def _use_temp_sqlite(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_BACKEND", "sqlite", raising=False)
    monkeypatch.setattr(config, "DATA_FILE", str(tmp_path / "legacy.json"), raising=False)
    monkeypatch.setattr(config, "SQLITE_DB_FILE", str(tmp_path / "state.sqlite3"), raising=False)


def test_new_datastore_starts_with_emergency_stop_active(tmp_path, monkeypatch):
    _use_temp_sqlite(tmp_path, monkeypatch)

    store = DataStore()

    assert store.state["emergency_stop"] is True
    assert store.state["emergency_stop_reason"] == "startup safety default"


def test_datastore_backfills_active_stop_when_persisted_state_has_no_stop_key(tmp_path, monkeypatch):
    _use_temp_sqlite(tmp_path, monkeypatch)
    store = DataStore()
    store._sqlite_store.save({"pending_transactions": []})

    restarted = DataStore()

    assert restarted.state["emergency_stop"] is True


def test_datastore_backfills_active_stop_from_legacy_json_import(tmp_path, monkeypatch):
    _use_temp_sqlite(tmp_path, monkeypatch)
    Path(config.DATA_FILE).write_text(json.dumps({"pending_transactions": []}), encoding="utf-8")

    store = DataStore()

    assert store.state["emergency_stop"] is True


def test_datastore_preserves_explicitly_cleared_persisted_stop(tmp_path, monkeypatch):
    _use_temp_sqlite(tmp_path, monkeypatch)
    store = DataStore()
    store.state["emergency_stop"] = False
    store.save()

    restarted = DataStore()

    assert restarted.state["emergency_stop"] is False


def test_datastore_recovers_corrupt_payload_from_latest_sqlite_backup(tmp_path, monkeypatch):
    _use_temp_sqlite(tmp_path, monkeypatch)
    store = DataStore()
    store.state["recovery_marker"] = "durable"
    store.save()
    store._create_backup(reason="recovery-test")

    with sqlite3.connect(config.SQLITE_DB_FILE) as db:
        db.execute("UPDATE bot_state SET payload = 'invalid-json' WHERE singleton = 1")

    recovered = DataStore()
    assert recovered.state["recovery_marker"] == "durable"


def test_datastore_refuses_to_downgrade_newer_database_schema(tmp_path, monkeypatch):
    _use_temp_sqlite(tmp_path, monkeypatch)
    DataStore()
    with sqlite3.connect(config.SQLITE_DB_FILE) as db:
        db.execute("PRAGMA user_version = 99")

    with pytest.raises(StateStoreError, match="newer than supported"):
        DataStore()


def test_corrupt_committed_sqlite_refuses_stale_json_backup_downgrade(tmp_path, monkeypatch):
    _use_temp_sqlite(tmp_path, monkeypatch)
    store = DataStore()
    store.state["emergency_stop"] = True
    store.state["pending_transactions"] = [{"signature": "durable-pending"}]
    store.save()

    backup_dir = tmp_path / "backups"
    for snapshot in backup_dir.glob("sniper_state_*.sqlite3"):
        snapshot.unlink()
    (backup_dir / "sniper_data_20260101_000000_legacy.json").write_text(
        json.dumps({"emergency_stop": False, "pending_transactions": []}),
        encoding="utf-8",
    )
    with sqlite3.connect(config.SQLITE_DB_FILE) as db:
        db.execute("UPDATE bot_state SET payload = 'invalid-json' WHERE singleton = 1")

    with pytest.raises(StateStoreCorruptError, match="refusing legacy JSON downgrade"):
        DataStore()


def test_pending_signature_commit_survives_abrupt_process_exit_and_restart(tmp_path, monkeypatch):
    _use_temp_sqlite(tmp_path, monkeypatch)
    child_env = os.environ.copy()
    child_env.update({
        "DATA_BACKEND": "sqlite",
        "DATA_FILE": str(tmp_path / "legacy.json"),
        "SQLITE_DB_FILE": str(tmp_path / "state.sqlite3"),
        "TRADING_MODE": "PAPER",
        "LIVE_EXECUTION_ENABLED": "false",
    })
    for secret_name in ("SOLANA_PRIVATE_KEY", "JUPITER_API_KEY", "HELIUS_API_KEY", "ALCHEMY_API_KEY"):
        child_env.pop(secret_name, None)
    child_code = """
import os
from monitoring_list import DataStore
store = DataStore()
store.record_pending_transaction({"signature": "crash-after-commit", "status": "submitted"})
os._exit(73)
"""
    repository_root = Path(__file__).resolve().parents[1]
    child = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=repository_root,
        env=child_env,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert child.returncode == 73

    restarted = DataStore()
    assert restarted.state["pending_transactions"] == [
        {"signature": "crash-after-commit", "status": "submitted"}
    ]

    from main import SniperBot

    class Notifier:
        messages = []

        async def notify(self, *args):
            self.messages.append(args)

    bot = object.__new__(SniperBot)
    bot.data_store = restarted
    bot.notifier = Notifier()
    import asyncio
    asyncio.run(bot._recover_pending_transactions())

    assert restarted.state["emergency_stop"] is True
    assert restarted.state["pending_transactions"]
    assert bot.notifier.messages
