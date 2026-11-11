import pytest

from security import token_security_allows_buy


def test_security_gate_rejects_risky_token_even_with_high_score():
    result = {"error": None, "is_risky": True, "security_score": 80, "is_honeypot": None}
    assert token_security_allows_buy(result, 60) is False


def test_security_gate_accepts_only_explicitly_safe_result_above_threshold():
    safe = {"error": None, "is_risky": False, "security_score": 80, "is_honeypot": None}
    assert token_security_allows_buy(safe, 60) is True
    assert token_security_allows_buy(safe, 85) is False


@pytest.mark.parametrize("result", [
    None,
    {},
    {"error": "provider_unavailable", "is_risky": True, "security_score": 100},
    {"error": None, "security_score": 100},
    {"error": None, "is_risky": False, "security_score": float("nan")},
    {"error": None, "is_risky": False, "security_score": 100, "is_honeypot": True},
])
def test_security_gate_fails_closed_on_incomplete_or_invalid_results(result):
    assert token_security_allows_buy(result, 60) is False
