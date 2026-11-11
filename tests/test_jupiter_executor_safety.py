import asyncio

import pytest

import jupiter_executor


class _FakePubkey:
    def __str__(self):
        return "fake-public-key"


class _FakeKeypair:
    def pubkey(self):
        return _FakePubkey()


def test_uncertain_jito_submission_never_falls_back_to_rpc(monkeypatch):
    calls = {"rpc": 0, "jito": 0}

    async def quote(*_args):
        return {
            "inAmount": "100",
            "outAmount": "200",
            "priceImpactPct": "0.01",
            "inputMint": "input-mint",
            "outputMint": "output-mint",
        }

    async def build(*_args):
        return "signed-later"

    def sign(*_args):
        return b"transaction", "signature", "blockhash"

    async def jito(*_args):
        calls["jito"] += 1
        raise TimeoutError("Jito response timed out")

    async def rpc(*_args):
        calls["rpc"] += 1
        return "signature"

    monkeypatch.setattr(jupiter_executor, "get_quote", quote)
    monkeypatch.setattr(jupiter_executor, "_build_swap_transaction", build)
    monkeypatch.setattr(jupiter_executor, "_sign_transaction", sign)
    monkeypatch.setattr(jupiter_executor, "_send_via_jito", jito)
    monkeypatch.setattr(jupiter_executor, "_send_via_rpc", rpc)
    monkeypatch.setattr(jupiter_executor.config, "JITO_ENABLED", True)

    with pytest.raises(jupiter_executor.SwapOutcomeUnknown):
        asyncio.run(
            jupiter_executor.execute_swap(
                "input-mint", "output-mint", 100, 100, _FakeKeypair(), use_jito=True
            )
        )

    assert calls == {"jito": 1, "rpc": 0}
