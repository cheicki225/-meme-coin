import pytest

import config


@pytest.mark.parametrize("value", ["NaN", "nan", "Infinity", "-Infinity", "0", "-1", "invalid"])
def test_circuit_breaker_threshold_must_be_finite_and_positive(monkeypatch, value):
    monkeypatch.setenv("CIRCUIT_BREAKER_USD", value)
    with pytest.raises(ValueError, match="CIRCUIT_BREAKER_USD must be a finite positive number"):
        config._positive_finite_float_env("CIRCUIT_BREAKER_USD", "150")


def test_circuit_breaker_threshold_accepts_finite_positive_value(monkeypatch):
    monkeypatch.setenv("CIRCUIT_BREAKER_USD", "125.5")
    assert config._positive_finite_float_env("CIRCUIT_BREAKER_USD", "150") == 125.5
