import asyncio

from copytrade_listener import CopyTradeListener


def _listener(fetch_results):
    listener = CopyTradeListener.__new__(CopyTradeListener)
    listener._recent_signatures = {}
    listener._processing_signatures = set()
    listener._creation_mode_wallets = set()
    listener._subscribed_wallets = set()
    listener.on_withdrawal = None
    listener.on_large_sol_transfer = None
    listener.on_dev_token_transfer = None
    listener.on_buy = None
    listener.on_sell = None
    calls = {"fetch": 0}

    async def fetch(_signature):
        calls["fetch"] += 1
        result = fetch_results.pop(0)
        return result

    listener._fetch_parsed_transaction = fetch
    listener._classify_transaction = lambda _parsed: None
    return listener, calls


def test_transient_parse_failure_does_not_poison_signature_cache():
    async def scenario():
        listener, calls = _listener([None, {"meta": {}, "transaction": {}}])
        await listener._process_transaction("sig-1")
        assert "sig-1" not in listener._recent_signatures
        await listener._process_transaction("sig-1")
        assert "sig-1" in listener._recent_signatures
        await listener._process_transaction("sig-1")
        assert calls["fetch"] == 2

    asyncio.run(scenario())


def test_concurrent_duplicate_notification_is_fetched_once():
    async def scenario():
        listener, calls = _listener([{"meta": {}, "transaction": {}}])
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_fetch(_signature):
            calls["fetch"] += 1
            entered.set()
            await release.wait()
            return {"meta": {}, "transaction": {}}

        listener._fetch_parsed_transaction = slow_fetch
        first = asyncio.create_task(listener._process_transaction("sig-2"))
        await entered.wait()
        await listener._process_transaction("sig-2")
        release.set()
        await first
        assert calls["fetch"] == 1
        assert "sig-2" in listener._recent_signatures
        assert not listener._processing_signatures

    asyncio.run(scenario())
