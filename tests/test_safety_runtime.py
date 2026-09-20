import pytest

from safety_runtime import make_idempotency_key, preflight_buy, snapshot_from_state


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
