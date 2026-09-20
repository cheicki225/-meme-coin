import pytest

from safety_runtime import (
    make_idempotency_key,
    mark_intent,
    preflight_buy,
    reserve_intent,
    snapshot_from_state,
)


def test_idempotency_key_is_deterministic():
    first = make_idempotency_key(
        side="BUY", token_mint="mint", source_wallet="wallet", event_id="event-1"
    )
    second = make_idempotency_key(
        side="buy", token_mint="mint", source_wallet="wallet", event_id="event-1"
    )
    assert first == second
    assert first.startswith("buy:")


def test_snapshot_defaults_conservatively():
    snapshot = snapshot_from_state({})
    assert snapshot.balance_sol == 0.0
    assert snapshot.open_positions == 0


def test_preflight_buy_accepts_safe_order():
    intent = preflight_buy(
        token_mint="mint",
        amount_sol=0.1,
        source_wallet="wallet",
        settings={
            "max_position_sol": 0.5,
            "max_daily_loss_sol": 1.0,
            "max_open_positions": 3,
            "min_balance_reserve_sol": 0.05,
        },
        state={"balance_sol": 2.0, "daily_pnl_sol": 0.0, "open_positions": []},
    )
    assert intent.side == "buy"
    assert intent.idempotency_key.startswith("buy:")


def test_preflight_buy_fails_closed_when_balance_is_missing():
    with pytest.raises(ValueError):
        preflight_buy(
            token_mint="mint",
            amount_sol=0.1,
            source_wallet="wallet",
            settings={"max_position_sol": 0.5},
            state={},
        )


def test_pending_intent_blocks_duplicate_submission():
    state = {}
    intent = preflight_buy(
        token_mint="mint",
        amount_sol=0.1,
        source_wallet="wallet",
        settings={"max_position_sol": 0.5},
        state={"balance_sol": 2.0, "open_positions": []},
        event_id="signal-1",
    )
    reserve_intent(state, intent)
    with pytest.raises(ValueError, match="Duplicate"):
        reserve_intent(state, intent)


def test_failed_intent_can_be_retried_but_unknown_cannot():
    state = {}
    intent = preflight_buy(
        token_mint="mint",
        amount_sol=0.1,
        source_wallet="wallet",
        settings={"max_position_sol": 0.5},
        state={"balance_sol": 2.0, "open_positions": []},
        event_id="signal-1",
    )
    reserve_intent(state, intent)
    mark_intent(state, intent.idempotency_key, "failed", error="definite failure")
    reserve_intent(state, intent)
    mark_intent(state, intent.idempotency_key, "unknown", error="ambiguous submission")
    with pytest.raises(ValueError, match="Duplicate"):
        reserve_intent(state, intent)


def test_confirmed_intent_keeps_signature():
    state = {}
    intent = preflight_buy(
        token_mint="mint",
        amount_sol=0.1,
        source_wallet="wallet",
        settings={"max_position_sol": 0.5},
        state={"balance_sol": 2.0, "open_positions": []},
        event_id="signal-1",
    )
    reserve_intent(state, intent)
    mark_intent(state, intent.idempotency_key, "confirmed", signature="abc123")
    record = state["safety_order_intents"][intent.idempotency_key]
    assert record["status"] == "confirmed"
    assert record["signature"] == "abc123"
