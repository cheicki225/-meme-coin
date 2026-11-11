import json
import os

import pytest

from monitoring_list import DataStore


def _store(path, state):
    store = DataStore.__new__(DataStore)
    store.path = str(path)
    store.state = state
    store._last_backup_time = 0
    store._backup_dir = str(path.parent / "backups")
    return store


def test_save_or_raise_atomically_persists_json(tmp_path):
    path = tmp_path / "nested" / "state.json"
    store = _store(path, {"safety_order_intents": {"buy:test": {"status": "pending"}}})

    store.save_or_raise()

    assert json.loads(path.read_text(encoding="utf-8")) == store.state
    assert list(path.parent.glob("state.json.*.tmp")) == []


def test_save_or_raise_preserves_previous_file_when_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"stable": true}', encoding="utf-8")
    store = _store(path, {"changed": True})

    def fail_replace(source, destination):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated disk failure"):
        store.save_or_raise()

    assert json.loads(path.read_text(encoding="utf-8")) == {"stable": True}
    assert list(tmp_path.glob("state.json.*.tmp")) == []
