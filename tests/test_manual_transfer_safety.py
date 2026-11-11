import pytest

from manual_transfer_safety import validate_manual_transfer


@pytest.mark.parametrize("mode,stop,pending,amount,maximum", [
    ("PAPER", False, False, 0.01, 0.1),
    ("LIVE", True, False, 0.01, 0.1),
    ("LIVE", False, True, 0.01, 0.1),
    ("LIVE", False, False, 0.11, 0.1),
    ("LIVE", False, False, float("nan"), 0.1),
])
def test_manual_transfer_policy_fails_closed(mode, stop, pending, amount, maximum):
    with pytest.raises(ValueError):
        validate_manual_transfer(mode, stop, pending, amount, maximum)


def test_manual_transfer_policy_accepts_bounded_live_amount():
    validate_manual_transfer("LIVE", False, False, 0.05, 0.1, execution_enabled=True)



def test_manual_transfer_requires_second_live_opt_in():
    with pytest.raises(ValueError, match="LIVE_EXECUTION_ENABLED"):
        validate_manual_transfer("LIVE", False, False, 0.05, 0.1)
