import asyncio
from types import SimpleNamespace

from copytrade_listener import CopyTradeListener
from helius_event_store import HeliusEventStore


def _listener(on_buy, tmp_path):
    store = HeliusEventStore(
        tmp_path / "events.sqlite3", retry_base_seconds=0.001, retry_max_seconds=0.01
    )
    listener = CopyTradeListener(
        SimpleNamespace(), on_buy=on_buy, on_sell=None, event_store=store
    )
    listener._subscribed_wallets = {"tracked-wallet"}
    listener._classify_transaction = lambda parsed: {
        "wallet": "tracked-wallet",
        "action": "buy",
        "token_mint": "token-mint",
    }
    return listener


def test_rpc_fetch_failure_is_retried_and_deduplicated_durably(tmp_path):
    async def scenario():
        attempts = 0
        buys = []

        async def fetch(signature):
            nonlocal attempts
            attempts += 1
            return None if attempts == 1 else {"parsed": True}

        async def on_buy(wallet, mint, signature):
            buys.append((wallet, mint, signature))

        listener = _listener(on_buy, tmp_path)
        listener._fetch_parsed_transaction = fetch

        await listener._process_transaction("signature-1")
        assert listener._event_store.checkpoint("copytrade:tracked-wallet") == (0, None)
        await asyncio.sleep(0.01)
        await listener._process_transaction("signature-1")

        assert attempts == 2
        assert buys == [("tracked-wallet", "token-mint", "signature-1")]
        assert listener._event_store.checkpoint("copytrade:tracked-wallet") == (
            1,
            "signature-1",
        )
        assert listener._event_store.metrics()["duplicates"] == 1

    asyncio.run(scenario())


def test_concurrent_duplicate_signature_is_not_claimed_twice(tmp_path):
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

        listener = _listener(on_buy, tmp_path)
        listener._fetch_parsed_transaction = fetch

        first = asyncio.create_task(listener._process_transaction("signature-2"))
        await started.wait()
        await listener._process_transaction("signature-2")
        release.set()
        await first

        assert attempts == 1
        assert buys == ["signature-2"]
        assert listener._event_store.checkpoint("copytrade:tracked-wallet") == (
            1,
            "signature-2",
        )

    asyncio.run(scenario())
