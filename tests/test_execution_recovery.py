import asyncio

import pytest

from execution_recovery import (
    classify_pending_transactions,
    inspect_signature_on_chain,
    reconcile_pending_transactions_readonly,
)
from monitoring_list import DataStore
from main import SniperBot


def test_reconciles_signatures_attached_to_persisted_positions():
    state = {
        "open_positions": [{"tx_signatures": ["buy-1"]}],
        "closed_positions": [{"tx_signatures": ["buy-2", "sell-2"]}],
        "pending_transactions": [
            {"signature": "buy-1", "status": "accounted"},
            {"signature": "sell-2", "status": "accounted"},
            {"signature": "unknown", "status": "submitted"},
        ],
    }
    reconciled, unresolved = classify_pending_transactions(state)
    assert reconciled == ["buy-1", "sell-2"]
    assert [item["signature"] for item in unresolved] == ["unknown"]


def test_malformed_pending_records_remain_unresolved():
    reconciled, unresolved = classify_pending_transactions({"pending_transactions": [None, {}]})
    assert reconciled == []
    assert len(unresolved) == 2


def test_malformed_signature_containers_never_reconcile_a_pending_transaction():
    state = {
        "open_positions": [{"tx_signatures": "signature-that-is-not-a-list"}],
        "closed_positions": "invalid-container",
        "pending_transactions": [{"signature": "signature-that-is-not-a-list"}],
    }
    reconciled, unresolved = classify_pending_transactions(state)
    assert reconciled == []
    assert unresolved == [{"signature": "signature-that-is-not-a-list"}]


def test_malformed_pending_collection_fails_closed():
    for malformed in (None, {"signature": "maybe-sent"}, "maybe-sent"):
        reconciled, unresolved = classify_pending_transactions({"pending_transactions": malformed})
        assert reconciled == []
        assert unresolved == [{"signature": None, "reason": "invalid_pending_transactions"}]


def test_restart_recovery_activates_stop_for_malformed_pending_collection():
    class Store:
        state = {"pending_transactions": None}

        def activate_emergency_stop(self, reason):
            self.state["emergency_stop"] = True
            self.state["emergency_stop_reason"] = reason

    class Notifier:
        messages = []

        async def notify(self, *_args):
            self.messages.append(_args)

    bot = object.__new__(SniperBot)
    bot.data_store = Store()
    bot.notifier = Notifier()
    asyncio.run(bot._recover_pending_transactions())

    assert bot.data_store.state["emergency_stop"] is True
    assert "unknown" in bot.notifier.messages[0][1]


def test_restart_recovery_halts_if_local_resolution_cannot_be_persisted():
    class Store:
        state = {
            "pending_transactions": [{"signature": "buy-1"}],
            "open_positions": [{"tx_signatures": ["buy-1"]}],
            "closed_positions": [],
        }

        def resolve_pending_transaction(self, *_args):
            raise OSError("disk unavailable")

        def activate_emergency_stop(self, reason):
            self.state["emergency_stop"] = True
            self.state["emergency_stop_reason"] = reason

    class Notifier:
        messages = []

        async def notify(self, *_args):
            self.messages.append(_args)

    bot = object.__new__(SniperBot)
    bot.data_store = Store()
    bot.notifier = Notifier()
    asyncio.run(bot._recover_pending_transactions())

    assert bot.data_store.state["emergency_stop"] is True
    assert bot.data_store.state["pending_transactions"]
    assert "suspendues" in bot.notifier.messages[0][1]


def _store(state, monkeypatch):
    store = object.__new__(DataStore)
    store.state = state
    monkeypatch.setattr(store, "save", lambda: None)
    return store


def test_inspector_requires_finalized_status_and_reads_transaction_fee():
    calls = []

    async def fake_rpc(payload, timeout):
        calls.append((payload, timeout))
        if payload["method"] == "getSignatureStatuses":
            return {"value": [{"confirmationStatus": "finalized", "err": {"InstructionError": [0, "Failed"]}}]}
        return {"meta": {"err": {"InstructionError": [0, "Failed"]}, "fee": 5000}}

    result = asyncio.run(inspect_signature_on_chain("known-signature", rpc_post=fake_rpc))
    assert result == {"status": "finalized_failed", "fee_lamports": 5000}
    assert [call[0]["method"] for call in calls] == ["getSignatureStatuses", "getTransaction"]


def test_unfinalized_or_missing_status_is_never_classified_as_failure():
    async def unfinalized(_payload, timeout):
        return {"value": [{"confirmationStatus": "confirmed", "err": None}]}

    async def missing(_payload, timeout):
        return {"value": [None]}

    assert asyncio.run(inspect_signature_on_chain("signature", rpc_post=unfinalized))["status"] == "pending"
    assert asyncio.run(inspect_signature_on_chain("signature", rpc_post=missing))["status"] == "unknown"


def test_reconcile_resolves_only_local_matches_and_finalized_failures(monkeypatch):
    store = _store({
        "open_positions": [{"tx_signatures": ["already-accounted"]}],
        "closed_positions": [],
        "pending_transactions": [
            {"signature": "already-accounted", "status": "accounted"},
            {"signature": "failed", "status": "submitted"},
            {"signature": "success-unaccounted", "status": "submitted"},
            {"signature": "still-unknown", "status": "submitted"},
        ],
        "resolved_transactions": [],
        "unapplied_failed_transaction_fees_lamports": 0,
    }, monkeypatch)
    results = {
        "failed": {"status": "finalized_failed", "fee_lamports": 7500},
        "success-unaccounted": {"status": "finalized_success", "fee_lamports": 5000},
        "still-unknown": {"status": "unknown"},
    }

    async def inspect(signature):
        return results[signature]

    summary = asyncio.run(reconcile_pending_transactions_readonly(store, inspector=inspect))
    assert summary["resolved_from_positions"] == 1
    assert summary["finalized_failures_resolved"] == 1
    assert summary["confirmed_unaccounted"] == 1
    assert summary["pending_or_unknown"] == 1
    assert summary["remaining"] == 2
    assert store.state["unapplied_failed_transaction_fees_lamports"] == 7500
    assert {item["signature"] for item in store.state["pending_transactions"]} == {
        "success-unaccounted", "still-unknown",
    }
    assert store.state["emergency_stop"] if "emergency_stop" in store.state else True


def test_confirmed_success_without_position_stays_pending(monkeypatch):
    store = _store({
        "open_positions": [], "closed_positions": [],
        "pending_transactions": [{"signature": "confirmed", "status": "submitted"}],
        "resolved_transactions": [],
    }, monkeypatch)
    summary = asyncio.run(reconcile_pending_transactions_readonly(
        store, inspector=lambda _signature: asyncio.sleep(0, result={"status": "finalized_success", "fee_lamports": 5000}),
    ))
    assert summary["remaining"] == 1
    assert store.state["pending_transactions"][0]["signature"] == "confirmed"


def test_failed_transaction_resolution_rolls_back_if_persistence_fails(monkeypatch):
    store = object.__new__(DataStore)
    store.state = {
        "pending_transactions": [{"signature": "failed", "status": "submitted"}],
        "resolved_transactions": [],
        "unapplied_failed_transaction_fees_lamports": 0,
    }

    def fail_save():
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "save", fail_save)
    with pytest.raises(OSError, match="disk unavailable"):
        store.resolve_pending_transaction("failed", {
            "chain_status": "finalized_failed", "fee_lamports": 5000,
        })
    assert [item["signature"] for item in store.state["pending_transactions"]] == ["failed"]
    assert store.state["resolved_transactions"] == []
    assert store.state["unapplied_failed_transaction_fees_lamports"] == 0

