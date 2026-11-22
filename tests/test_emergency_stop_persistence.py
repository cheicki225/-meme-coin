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


def test_failed_halt_persistence_keeps_emergency_stop_active_in_memory(monkeypatch):
    store = object.__new__(DataStore)
    store.state = {"emergency_stop": False, "pending_transactions": []}

    def fail_save():
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "save", fail_save)
    with pytest.raises(OSError, match="disk unavailable"):
        store.activate_emergency_stop("manual halt")

    assert store.state["emergency_stop"] is True
    assert assess_new_position(
        store.state,
        10,
        max_exposure_usd=100,
        max_open_positions=5,
    ) == (False, "emergency_stop_active")


@pytest.mark.parametrize("operation", ["record", "account", "resolve"])
def test_pending_transaction_mutations_roll_back_on_persistence_failure(monkeypatch, operation):
    store = object.__new__(DataStore)
    store.state = {
        "pending_transactions": [{"signature": "sig-1", "status": "submitted"}],
        "resolved_transactions": [],
        "unapplied_failed_transaction_fees_lamports": 0,
    }

    def fail_save():
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "save", fail_save)
    with pytest.raises(OSError, match="disk unavailable"):
        if operation == "record":
            store.record_pending_transaction({"signature": "sig-2", "status": "submitted"})
        elif operation == "account":
            store.mark_pending_transaction_accounted("sig-1", {"fee": 1})
        else:
            store.resolve_pending_transaction("sig-1", {"chain_status": "already_accounted"})

    assert store.state["pending_transactions"] == [{"signature": "sig-1", "status": "submitted"}]
    assert store.state["resolved_transactions"] == []


def test_malformed_resolution_history_cannot_remove_pending_signature(monkeypatch):
    store = object.__new__(DataStore)
    store.state = {
        "pending_transactions": [{"signature": "sig-1", "status": "submitted"}],
        "resolved_transactions": None,
    }
    monkeypatch.setattr(store, "save", lambda: None)

    with pytest.raises(ValueError, match="resolved_transactions"):
        store.resolve_pending_transaction("sig-1")

    assert store.state["pending_transactions"] == [{"signature": "sig-1", "status": "submitted"}]
