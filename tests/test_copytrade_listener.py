import asyncio
from types import SimpleNamespace

from copytrade_listener import CopyTradeListener


def _listener(on_buy):
    listener = CopyTradeListener(SimpleNamespace(), on_buy=on_buy, on_sell=None)
    listener._subscribed_wallets = {"tracked-wallet"}
    listener._classify_transaction = lambda parsed: {
        "wallet": "tracked-wallet",
        "action": "buy",
        "token_mint": "token-mint",
    }
    return listener


def test_rpc_fetch_failure_does_not_poison_signature_deduplication():
    async def scenario():
        attempts = 0
        buys = []

        async def fetch(signature):
            nonlocal attempts
            attempts += 1
            return None if attempts == 1 else {"parsed": True}

        async def on_buy(wallet, mint, signature):
            buys.append((wallet, mint, signature))

        listener = _listener(on_buy)
        listener._fetch_parsed_transaction = fetch

        await listener._process_transaction("signature-1")
        await listener._process_transaction("signature-1")
        await listener._process_transaction("signature-1")

        assert attempts == 2
        assert buys == [("tracked-wallet", "token-mint", "signature-1")]
        assert "signature-1" in listener._recent_signatures
        assert not listener._processing_signatures

    asyncio.run(scenario())


def test_concurrent_duplicate_signature_is_fetched_and_signaled_once():
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        attempts = 0
        buys = []

        async def fetch(signature):
            nonlocal attempts
            attempts += 1
            started.set()
            await release.wait()
            return {"parsed": True}

        async def on_buy(wallet, mint, signature):
            buys.append(signature)

        listener = _listener(on_buy)
        listener._fetch_parsed_transaction = fetch

        first = asyncio.create_task(listener._process_transaction("signature-2"))
        await started.wait()
        await listener._process_transaction("signature-2")
        release.set()
        await first

        assert attempts == 1
        assert buys == ["signature-2"]
        assert not listener._processing_signatures

    asyncio.run(scenario())
