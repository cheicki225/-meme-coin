import asyncio

import pytest

import config
from jupiter_executor import SwapError, _build_swap_transaction, _validate_order, _validate_quote, execute_swap, get_quote


WALLET = "Wallet111111111111111111111111111111111111"
OTHER = "Other1111111111111111111111111111111111111"


def _order(**updates):
    order = {
        "inputMint": config.SOL_MINT,
        "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "inAmount": "1000000",
        "outAmount": "900000",
        "otherAmountThreshold": "850000",
        "slippageBps": 100,
        "priceImpact": -1.5,
        "router": "metis",
        "taker": WALLET,
        "gasless": False,
        "requestId": "order-1",
        "lastValidBlockHeight": "1000",
        "rentFeeLamports": 2_000_000,
        "signatureFeePayer": WALLET,
        "prioritizationFeePayer": WALLET,
        "rentFeePayer": WALLET,
        "transaction": "unsigned-base64",
    }
    order.update(updates)
    return order


def test_v2_order_accepts_negative_price_impact_as_a_bounded_percent():
    quote = _order()
    _validate_quote(quote, config.SOL_MINT, quote["outputMint"], 1_000_000, 100)
    _validate_order(quote, WALLET)


@pytest.mark.parametrize("changes", [
    {"router": "jupiterz"},
    {"taker": OTHER},
    {"gasless": True},
    {"rentFeeLamports": 5_000_001},
    {"rentFeePayer": OTHER},
    {"requestId": ""},
    {"lastValidBlockHeight": "0"},
])
def test_v2_order_rejects_unsafe_route_or_fee_metadata(changes, monkeypatch):
    monkeypatch.setattr(config, "MAX_JUPITER_RENT_LAMPORTS", 5_000_000, raising=False)
    with pytest.raises(SwapError):
        _validate_order(_order(**changes), WALLET)


def test_v2_transaction_is_taken_only_from_validated_order():
    assert asyncio.run(_build_swap_transaction(_order(), WALLET)) == "unsigned-base64"


def test_quote_requires_api_key_before_network_access(monkeypatch):
    monkeypatch.setattr(config, "JUPITER_API_KEY", "", raising=False)
    with pytest.raises(SwapError, match="JUPITER_API_KEY"):
        asyncio.run(get_quote(config.SOL_MINT, _order()["outputMint"], 1_000_000, 100, WALLET))


def test_executor_refuses_live_mode_without_second_opt_in(monkeypatch):
    monkeypatch.setattr(config, "TRADING_MODE", "LIVE", raising=False)
    monkeypatch.setattr(config, "LIVE_EXECUTION_ENABLED", False, raising=False)
    with pytest.raises(SwapError, match="LIVE execution is disabled"):
        asyncio.run(execute_swap(config.SOL_MINT, _order()["outputMint"], 1_000_000, 100))
