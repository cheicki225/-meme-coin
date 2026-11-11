import asyncio

import pytest

import jupiter_executor as executor


class FakeKeypair:
    def pubkey(self):
        return "WalletPubkey"


def _quote(**overrides):
    quote = {
        "inputMint": "So11111111111111111111111111111111111111112",
        "outputMint": "TokenMint",
        "inAmount": "100000000",
        "outAmount": "250000",
        "priceImpactPct": "0.01",
    }
    quote.update(overrides)
    return quote


def test_execute_swap_rejects_malformed_quote_before_sign_or_send(monkeypatch):
    sent = {"value": False}

    async def quote(*args, **kwargs):
        return _quote(outAmount="0", priceImpactPct="NaN")

    async def build(*args, **kwargs):
        raise AssertionError("must not build a transaction from an invalid quote")

    async def send(*args, **kwargs):
        sent["value"] = True

    monkeypatch.setattr(executor, "get_quote", quote)
    monkeypatch.setattr(executor, "_build_swap_transaction", build)
    monkeypatch.setattr(executor, "_send_via_rpc", send)

    with pytest.raises(executor.SwapError):
        asyncio.run(executor.execute_swap(
            "So11111111111111111111111111111111111111112",
            "TokenMint", 100_000_000, 500, keypair=FakeKeypair()
        ))
    assert sent["value"] is False


def test_execute_swap_rejects_quote_for_wrong_mints(monkeypatch):
    async def quote(*args, **kwargs):
        return _quote(outputMint="DifferentMint")

    monkeypatch.setattr(executor, "get_quote", quote)
    with pytest.raises(executor.SwapError, match="mints"):
        asyncio.run(executor.execute_swap(
            "So11111111111111111111111111111111111111112",
            "TokenMint", 100_000_000, 500, keypair=FakeKeypair()
        ))


def test_execute_swap_accepts_complete_quote_and_returns_validated_amounts(monkeypatch):
    async def quote(*args, **kwargs):
        return _quote()

    async def build(*args, **kwargs):
        return "signed-build-placeholder"

    def sign(*args, **kwargs):
        return b"signed", "signature-1", "blockhash-1"

    async def send(*args, **kwargs):
        return "signature-1"

    async def confirm(*args, **kwargs):
        return True

    monkeypatch.setattr(executor, "get_quote", quote)
    monkeypatch.setattr(executor, "_build_swap_transaction", build)
    monkeypatch.setattr(executor, "_sign_transaction", sign)
    monkeypatch.setattr(executor, "_send_via_rpc", send)
    monkeypatch.setattr(executor, "_confirm_transaction", confirm)

    result = asyncio.run(executor.execute_swap(
        "So11111111111111111111111111111111111111112",
        "TokenMint", 100_000_000, 500, keypair=FakeKeypair()
    ))
    assert result["signature"] == "signature-1"
    assert result["input_amount"] == 100_000_000
    assert result["output_amount"] == 250_000
    assert result["price_impact_pct"] == pytest.approx(1.0)
