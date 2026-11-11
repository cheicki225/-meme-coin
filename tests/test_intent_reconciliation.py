import pytest

from intent_reconciliation import list_unresolved_intents, resolve_intent


def test_list_unresolved_intents_returns_pending_and_unknown():
    state = {
        "safety_order_intents": {
            "b": {"status": "unknown", "created_at": 2},
            "a": {"status": "pending", "created_at": 1},
            "c": {"status": "confirmed", "created_at": 0},
        }
    }
    result = list_unresolved_intents(state)
    assert [item["idempotency_key"] for item in result] == ["a", "b"]


def test_resolve_intent_records_operator_resolution():
    state = {"safety_order_intents": {"key": {"status": "unknown"}}}
    resolve_intent(state, "key", status="confirmed", signature="sig", note="verified on-chain")
    assert state["safety_order_intents"]["key"] == {
        "status": "confirmed",
        "signature": "sig",
        "reconciliation_note": "verified on-chain",
    }


def test_resolve_intent_rejects_invalid_status_and_unknown_key():
    state = {"safety_order_intents": {"key": {"status": "unknown"}}}
    with pytest.raises(ValueError):
        resolve_intent(state, "key", status="pending")
    with pytest.raises(ValueError):
        resolve_intent(state, "missing", status="failed")



def test_resolve_intent_cannot_downgrade_terminal_intent():
    state = {"safety_order_intents": {"key": {"status": "confirmed", "signature": "sig"}}}
    with pytest.raises(ValueError, match="Only pending or unknown"):
        resolve_intent(state, "key", status="failed")
    assert state["safety_order_intents"]["key"]["status"] == "confirmed"


def test_unknown_is_not_a_resolution_outcome():
    state = {"safety_order_intents": {"key": {"status": "pending"}}}
    with pytest.raises(ValueError, match="establish confirmed or failed"):
        resolve_intent(state, "key", status="unknown")
    assert state["safety_order_intents"]["key"]["status"] == "pending"


def test_malformed_created_at_does_not_break_unresolved_intent_listing():
    state = {
        "safety_order_intents": {
            "bad": {"status": "unknown", "created_at": "not-a-timestamp"},
            "good": {"status": "pending", "created_at": 10},
        }
    }
    result = list_unresolved_intents(state)
    assert {item["idempotency_key"] for item in result} == {"bad", "good"}
