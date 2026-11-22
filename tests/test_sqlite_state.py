import json
import sqlite3
from contextlib import contextmanager

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


def test_pending_transaction_records_survive_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    state = {
        "pending_transactions": [{
            "signature": "pending-signature",
            "kind": "buy",
            "status": "submitted",
        }],
    }
    SQLiteStateStore(path).save(state)

    assert SQLiteStateStore(path).load(dict) == state


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


def test_refuses_newer_row_schema_without_falling_back_to_old_payload(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = SQLiteStateStore(path)
    store.save({"preserve": "this row"})
    with sqlite3.connect(path) as db:
        db.execute("UPDATE bot_state SET schema_version = 2 WHERE singleton = 1")

    with pytest.raises(StateStoreError, match="newer than supported"):
        SQLiteStateStore(path).load(dict)

    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT schema_version, payload FROM bot_state WHERE singleton = 1"
        ).fetchone() == (2, '{"preserve":"this row"}')


def test_rejects_invalid_row_schema_and_nonstandard_json_constants(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = SQLiteStateStore(path)
    store.save({"safe": True})
    with sqlite3.connect(path) as db:
        db.execute("UPDATE bot_state SET schema_version = 0 WHERE singleton = 1")
    with pytest.raises(StateStoreCorruptError, match="schema version is invalid"):
        SQLiteStateStore(path).load(dict)

    with sqlite3.connect(path) as db:
        db.execute("UPDATE bot_state SET schema_version = 1, payload = '{\"value\":NaN}' WHERE singleton = 1")
    with pytest.raises(StateStoreCorruptError, match="invalid JSON"):
        SQLiteStateStore(path).load(dict)

    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE bot_state SET payload = '{\"emergency_stop\":false,\"emergency_stop\":true}' "
            "WHERE singleton = 1"
        )
    with pytest.raises(StateStoreCorruptError, match="invalid JSON"):
        SQLiteStateStore(path).load(dict)


def test_migration_is_idempotent_and_preserves_committed_state(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = SQLiteStateStore(path)
    store.save({"cursor": "signature-1", "emergency_stop": True})

    # Simulate an existing database that has the current table but has not yet
    # recorded its migration version. Initialization should only advance the
    # marker and preserve the already committed row.
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 0")

    assert SQLiteStateStore(path).load(dict) == {
        "cursor": "signature-1",
        "emergency_stop": True,
    }
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_only_actual_sqlite_corruption_is_eligible_for_backup_recovery():
    busy = sqlite3.OperationalError("database is locked")
    malformed = sqlite3.DatabaseError("database disk image is malformed")
    assert SQLiteStateStore._is_corruption_error(busy) is False
    assert SQLiteStateStore._is_corruption_error(malformed) is True


@pytest.mark.parametrize("method", ["load", "has_committed_state"])
def test_transient_database_errors_are_not_classified_as_corruption(tmp_path, monkeypatch, method):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")

    @contextmanager
    def locked_connection():
        raise sqlite3.OperationalError("database is locked")
        yield  # pragma: no cover - keeps this function a context manager

    monkeypatch.setattr(store, "_connection", locked_connection)
    with pytest.raises(StateStoreError) as raised:
        getattr(store, method)(dict) if method == "load" else store.has_committed_state()
    assert not isinstance(raised.value, StateStoreCorruptError)


def test_legacy_json_with_duplicate_keys_is_rejected_without_overwriting_file(tmp_path):
    legacy = tmp_path / "legacy.json"
    original = '{"emergency_stop":false,"emergency_stop":true}'
    legacy.write_text(original, encoding="utf-8")
    store = SQLiteStateStore(tmp_path / "state.sqlite3")

    with pytest.raises(StateStoreError, match="could not be imported"):
        store.load(dict, legacy_json_path=legacy)

    assert legacy.read_text(encoding="utf-8") == original
    assert not store.has_committed_state()


def test_legacy_json_with_wrong_root_type_is_not_silently_replaced(tmp_path):
    legacy = tmp_path / "legacy.json"
    original = '[{"pending_transactions":[{"signature":"maybe"}]}]'
    legacy.write_text(original, encoding="utf-8")
    store = SQLiteStateStore(tmp_path / "state.sqlite3")

    with pytest.raises(StateStoreError, match="could not be imported"):
        store.load(dict, legacy_json_path=legacy)

    assert legacy.read_text(encoding="utf-8") == original
    assert not store.has_committed_state()

