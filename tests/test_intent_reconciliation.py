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

from intent_reconciliation import list_unresolved_sell_intents, resolve_sell_intent


def _sell_state():
    return {
        "safety_pending_sell_tokens": {
            "MintA": {"intent_id": "intent-a", "status": "unknown", "created_at": 10, "units": 2.0}
        },
        "safety_unresolved_sell_tokens": {
            "MintA": {"intent_id": "intent-a", "status": "unknown", "created_at": 10}
        },
        "safety_unaccounted_confirmed_sells": {},
    }


def test_list_unresolved_sell_intents_is_read_only_and_includes_token():
    state = _sell_state()
    items = list_unresolved_sell_intents(state)
    assert any(item["token_mint"] == "MintA" and item["intent_id"] == "intent-a" for item in items)
    assert state["safety_pending_sell_tokens"]["MintA"]["status"] == "unknown"


def test_confirmed_sell_reconciliation_keeps_token_blocked_for_accounting():
    state = _sell_state()
    resolve_sell_intent(state, "MintA", status="confirmed", signature="verified-sig")
    assert state["safety_pending_sell_tokens"]["MintA"]["status"] == "confirmed_awaiting_accounting"
    assert state["safety_pending_sell_tokens"]["MintA"]["signature"] == "verified-sig"
    assert state["safety_unaccounted_confirmed_sells"]["MintA"]["token_mint"] == "MintA"
    assert "MintA" not in state["safety_unresolved_sell_tokens"]


def test_failed_sell_reconciliation_releases_only_after_operator_verified_failure():
    state = _sell_state()
    resolve_sell_intent(state, "MintA", status="failed", note="verified failed on chain")
    assert "MintA" not in state["safety_pending_sell_tokens"]
    assert "MintA" not in state["safety_unresolved_sell_tokens"]


def test_confirmed_sell_requires_signature_and_unknown_is_not_resolution():
    state = _sell_state()
    with pytest.raises(ValueError, match="signature"):
        resolve_sell_intent(state, "MintA", status="confirmed")
    with pytest.raises(ValueError, match="verified confirmed or failed"):
        resolve_sell_intent(state, "MintA", status="unknown")
    assert state["safety_pending_sell_tokens"]["MintA"]["status"] == "unknown"
