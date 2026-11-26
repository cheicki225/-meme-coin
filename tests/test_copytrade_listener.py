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

        async def on_buy(wallet, mint, signature, *, event_record):
            buys.append((wallet, mint, signature, event_record))

        listener = _listener(on_buy, tmp_path)
        listener._fetch_parsed_transaction = fetch

        await listener._process_transaction(
            "signature-1", source="copytrade:tracked-wallet", slot=42,
            transaction_index=0, is_replay=True,
        )
        assert listener._event_store.checkpoint("copytrade:tracked-wallet") == (0, None)
        await asyncio.sleep(0.01)
        await listener._process_transaction(
            "signature-1", source="copytrade:tracked-wallet", slot=42,
            transaction_index=0, is_replay=True,
        )

        assert attempts == 2
        assert len(buys) == 1
        wallet, mint, signature, event_record = buys[0]
        assert (wallet, mint, signature) == (
            "tracked-wallet", "token-mint", "signature-1"
        )
        assert event_record.signature == signature
        assert event_record.stream == "copytrade:tracked-wallet"
        assert event_record.slot == 42
        assert event_record.transaction_index == 0
        # This synthetic direct enqueue carries a slot/index fixture only;
        # it does not prove provider finality or history completeness.
        assert "canonical_order_evidence" not in event_record.payload
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

        async def on_buy(wallet, mint, signature, *, event_record):
            buys.append((signature, event_record.event_id))

        listener = _listener(on_buy, tmp_path)
        listener._fetch_parsed_transaction = fetch

        first = asyncio.create_task(listener._process_transaction(
            "signature-2", source="copytrade:tracked-wallet", slot=42,
            transaction_index=1, is_replay=True,
        ))
        await started.wait()
        await listener._process_transaction(
            "signature-2", source="copytrade:tracked-wallet", slot=42,
            transaction_index=1, is_replay=True,
        )
        release.set()
        await first

        assert attempts == 1
        assert len(buys) == 1
        assert buys[0][0] == "signature-2"
        assert type(buys[0][1]) is int
        assert listener._event_store.checkpoint("copytrade:tracked-wallet") == (
            1,
            "signature-2",
        )

    asyncio.run(scenario())


def test_explicit_unknown_buy_callback_keeps_event_unacknowledged(tmp_path):
    async def on_buy(_wallet, _mint, _signature, *, event_record):
        assert event_record.signature == "signature-unknown"
        return SimpleNamespace(status="unknown", reason="provenance_unverified")

    listener = _listener(on_buy, tmp_path)

    async def fetch(_signature):
        return {"parsed": True}

    listener._fetch_parsed_transaction = fetch

    asyncio.run(
        listener._process_transaction(
            "signature-unknown",
            source="copytrade:tracked-wallet",
            slot=42,
            transaction_index=2,
            is_replay=True,
        )
    )

    stream = "copytrade:tracked-wallet"
    assert listener._event_store.has_unknown(stream)
    assert listener._event_store.checkpoint(stream) == (0, None)
    assert listener._event_store.intake_paused(stream)
    assert listener._event_store.blocked_reason(stream) == "provenance_unverified"
