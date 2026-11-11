from risk_controls import assess_new_position


def test_blocks_entries_after_emergency_stop_or_loss_limit_activation():
    allowed, reason = assess_new_position(
        {"emergency_stop": True, "open_positions": []}, 10,
        max_exposure_usd=100, max_open_positions=5,
    )
    assert not allowed
    assert reason == "emergency_stop_active"


def test_counts_open_and_concurrent_reserved_exposure():
    state = {"open_positions": [{"cost_basis_usd": 40}]}
    allowed, reason = assess_new_position(
        state, 20, max_exposure_usd=100, max_open_positions=5,
        reserved_exposure_usd=45,
    )
    assert not allowed
    assert reason == "max_open_exposure_reached"


def test_enforces_position_count_and_rejects_corrupt_position_state():
    allowed, reason = assess_new_position(
        {"open_positions": [{"cost_basis_usd": 1}]}, 1,
        max_exposure_usd=100, max_open_positions=1,
    )
    assert (allowed, reason) == (False, "max_open_positions_reached")

    allowed, reason = assess_new_position(
        {"open_positions": [{"cost_basis_usd": float("nan")}]}, 1,
        max_exposure_usd=100, max_open_positions=2,
    )
    assert (allowed, reason) == (False, "invalid_position_exposure")


def test_unknown_existing_exposure_blocks_new_entries():
    assert assess_new_position(
        {"open_positions": [{"token_mint": "unknown"}]},
        10,
        max_exposure_usd=250,
        max_open_positions=10,
    ) == (False, "invalid_position_exposure")


def test_invalid_position_reservation_fails_closed():
    assert assess_new_position(
        {"open_positions": []},
        10,
        max_exposure_usd=250,
        max_open_positions=10,
        reserved_positions=-1,
    ) == (False, "invalid_reserved_positions")

