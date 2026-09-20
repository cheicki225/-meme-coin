import pytest

from safety_guard import OrderLimits, SafetySnapshot, validate_order


LIMITS = OrderLimits(
    max_position_sol=0.5,
    max_daily_loss_sol=1.0,
    max_open_positions=3,
    min_balance_reserve_sol=0.05,
)
SNAPSHOT = SafetySnapshot(balance_sol=2.0, daily_pnl_sol=0.0, open_positions=0)


def test_valid_order():
    validate_order(0.1, LIMITS, SNAPSHOT, idempotency_key="token:signature")


@pytest.mark.parametrize(
    "amount, snapshot, message",
    [
        (0.6, SNAPSHOT, "max position"),
        (0.1, SafetySnapshot(2.0, 0.0, 3), "open positions"),
        (0.1, SafetySnapshot(2.0, -1.0, 0), "Daily loss"),
        (0.1, SafetySnapshot(0.1, 0.0, 0, 0.0), "reserve"),
    ],
)
def test_limits_fail_closed(amount, snapshot, message):
    with pytest.raises(ValueError, match=message):
        validate_order(amount, LIMITS, snapshot, idempotency_key="token:signature")


def test_emergency_stop_and_idempotency():
    with pytest.raises(ValueError, match="Emergency"):
        validate_order(0.1, LIMITS, SNAPSHOT, emergency_stop=True, idempotency_key="x")
    with pytest.raises(ValueError, match="idempotency"):
        validate_order(0.1, LIMITS, SNAPSHOT)
