import math

import pytest

from copytrade_listener import is_token_age_within_limit


@pytest.mark.parametrize(
    ("age", "limit", "expected"),
    [
        (0, 5, True),
        (4.9, 5, True),
        (5, 5, True),
        (5.1, 5, False),
        (None, 5, False),
        (True, 5, False),
        (-1, 5, False),
        (math.nan, 5, False),
        (math.inf, 5, False),
        (10**400, 5, False),
        ("4", 5, False),
        (None, 0, True),
    ],
)
def test_token_age_limit_fails_closed_for_unknown_or_invalid_age(age, limit, expected):
    assert is_token_age_within_limit(age, limit) is expected


@pytest.mark.parametrize("limit", [None, True, -1, math.nan, math.inf, 10**400, "5"])
def test_invalid_token_age_limit_is_rejected(limit):
    assert is_token_age_within_limit(1, limit) is False
