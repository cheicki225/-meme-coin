import pytest

from monitoring_list import DataStore
from risk_controls import assess_new_position


def test_failed_resume_persistence_restores_emergency_stop_in_memory(monkeypatch):
    store = object.__new__(DataStore)
    store.state = {
        "emergency_stop": True,
        "emergency_stop_reason": "manual halt",
        "emergency_stop_at": 123,
        "pending_transactions": [],
        "open_positions": [],
    }

    def fail_save():
        raise OSError("temporary persistence failure")

    monkeypatch.setattr(store, "save", fail_save)

    with pytest.raises(OSError, match="persistence failure"):
        store.clear_emergency_stop()

    assert store.state["emergency_stop"] is True
    assert store.state["emergency_stop_reason"] == "manual halt"
    assert store.state["emergency_stop_at"] == 123
    assert assess_new_position(
        store.state,
        10,
        max_exposure_usd=100,
        max_open_positions=5,
    ) == (False, "emergency_stop_active")
