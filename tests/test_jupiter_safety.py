import pytest

import config
from jupiter_executor import SwapError, _validate_quote


def _quote(**updates):
    quote = {
        "inputMint": "So11111111111111111111111111111111111111112",
        "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "inAmount": "1000000",
        "outAmount": "900000",
        "otherAmountThreshold": "850000",
        "slippageBps": 500,
        "priceImpactPct": "0.01",
    }
    quote.update(updates)
    return quote


def test_quote_accepts_matching_mints_amount_and_limits():
    _validate_quote(
        _quote(),
        "So11111111111111111111111111111111111111112",
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        1_000_000,
        500,
    )


@pytest.mark.parametrize("field,value", [
    ("inputMint", "11111111111111111111111111111111"),
    ("inAmount", "1000001"),
    ("otherAmountThreshold", "0"),
    ("slippageBps", 99999),
    ("priceImpactPct", "0.20"),
])
def test_quote_rejects_mismatches_or_excessive_risk(field, value):
    with pytest.raises(SwapError):
        _validate_quote(
            _quote(**{field: value}),
            "So11111111111111111111111111111111111111112",
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            1_000_000,
            500,
        )


def test_quote_obeys_configured_slippage_cap(monkeypatch):
    monkeypatch.setattr(config, "MAX_SLIPPAGE_BPS", 100)
    with pytest.raises(SwapError, match="Slippage"):
        _validate_quote(
            _quote(),
            "So11111111111111111111111111111111111111112",
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            1_000_000,
            500,
        )
