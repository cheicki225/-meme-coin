import json
import sqlite3

import pytest

import config
from monitoring_list import DataStore
from sqlite_state import StateStoreError


def _use_temp_sqlite(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_BACKEND", "sqlite", raising=False)
    monkeypatch.setattr(config, "DATA_FILE", str(tmp_path / "legacy.json"), raising=False)
    monkeypatch.setattr(config, "SQLITE_DB_FILE", str(tmp_path / "state.sqlite3"), raising=False)


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
