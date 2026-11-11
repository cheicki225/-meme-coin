import json
import sqlite3

import pytest

from sqlite_state import SQLiteStateStore, StateStoreCorruptError, StateStoreError


def test_imports_legacy_json_once_and_recovers_after_restart(tmp_path):
    legacy = tmp_path / "sniper_data.json"
    legacy.write_text(json.dumps({"open_positions": [{"mint": "abc"}]}), encoding="utf-8")
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    assert store.load(dict, legacy_json_path=legacy) == {"open_positions": [{"mint": "abc"}]}

    store.save({"open_positions": [], "total_pnl_usd": 12.5})
    restarted = SQLiteStateStore(tmp_path / "state.sqlite3")
    assert restarted.load(dict, legacy_json_path=legacy) == {"open_positions": [], "total_pnl_usd": 12.5}
    assert json.loads(legacy.read_text(encoding="utf-8"))["open_positions"] == [{"mint": "abc"}]


def test_can_distinguish_empty_database_from_committed_state(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    assert store.has_committed_state() is False
    store.save({"ready": True})
    assert store.has_committed_state() is True


def test_creates_consistent_backup_and_rejects_invalid_payload(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = SQLiteStateStore(path)
    store.save({"emergency_stop": True})
    backup = store.backup_to(tmp_path / "backups" / "state.sqlite3")
    assert SQLiteStateStore(backup).load(dict)["emergency_stop"] is True

    with sqlite3.connect(path) as db:
        db.execute("UPDATE bot_state SET payload = 'not-json' WHERE singleton = 1")
    with pytest.raises(StateStoreCorruptError, match="invalid JSON"):
        SQLiteStateStore(path).load(dict)


def test_rejects_non_json_state_without_overwriting_previous_commit(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    store.save({"version": 1})
    with pytest.raises(StateStoreError):
        store.save({"bad": float("nan")})
    assert SQLiteStateStore(tmp_path / "state.sqlite3").load(dict) == {"version": 1}


def test_refuses_newer_schema_without_downgrading_it(tmp_path):
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 42")

    with pytest.raises(StateStoreError, match="newer than supported"):
        SQLiteStateStore(path)

    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 42

