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



def test_corrupt_state_blocks_live_startup(tmp_path, monkeypatch):
    import monitoring_list

    path = tmp_path / "state.json"
    path.write_text("{ definitely not valid json", encoding="utf-8")
    monkeypatch.setattr(monitoring_list.config, "TRADING_MODE", "LIVE")

    with pytest.raises(RuntimeError, match="LIVE startup blocked"):
        DataStore(str(path))


def test_corrupt_state_keeps_paper_recovery_behavior(tmp_path, monkeypatch):
    import monitoring_list

    path = tmp_path / "state.json"
    path.write_text("{ definitely not valid json", encoding="utf-8")
    monkeypatch.setattr(monitoring_list.config, "TRADING_MODE", "PAPER")

    store = DataStore(str(path))
    assert isinstance(store.state, dict)
    assert store.state["open_positions"] == []



@pytest.mark.parametrize(
    "state_text",
    [
        '{"open_positions": "not-a-list"}',
        '{"open_positions": [], "safety_order_intents": []}',
        '{"open_positions": [], "safety_order_intents": {"bad": {"side": "buy", "status": "unknown", "amount_sol": "NaN"}}}',
    ],
)
def test_malformed_safety_state_blocks_live_startup(tmp_path, monkeypatch, state_text):
    import monitoring_list

    path = tmp_path / "state.json"
    path.write_text(state_text, encoding="utf-8")
    monkeypatch.setattr(monitoring_list.config, "TRADING_MODE", "LIVE")

    with pytest.raises(RuntimeError, match="LIVE startup blocked"):
        DataStore(str(path))
