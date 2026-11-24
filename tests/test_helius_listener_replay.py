import asyncio
import json
from types import SimpleNamespace

import protection_scanner
import websocket_listener
from copytrade_listener import CopyTradeListener
from helius_event_store import HeliusEventStore
from execution_metrics import summarize_metrics
from protection_scanner import ProtectionScanner
from websocket_listener import NewTokenListener


def _notification(signature: str, subscription: int) -> str:
    return json.dumps(
        {
            "method": "logsNotification",
            "params": {
                "subscription": subscription,
                "result": {
                    "context": {"slot": 42},
                    "value": {
                        "signature": signature,
                        "logs": ["Program log: Instruction: Create"],
                    },
                },
            },
        }
    )


def test_new_token_ws_event_retries_429_and_completes_correlated_replay(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    store = HeliusEventStore(
        tmp_path / "events.sqlite3", retry_base_seconds=0.001, retry_max_seconds=0.01
    )
    callbacks = []

    async def on_new_token(event):
        callbacks.append(event)

    listener = NewTokenListener(on_new_token, event_store=store)
    listener._sub_id_to_stream[7] = "new-token:program"
    listener._extract_creation_info = lambda _parsed: ("mint-public", "dev-public")
    fetches = 0

    async def fetch(_signature):
        nonlocal fetches
        fetches += 1
        if fetches == 1:
            return {}
        return {"slot": 42, "events": {}}

    monkeypatch.setattr(listener, "_fetch_parsed_transaction", fetch)

    async def scenario():
        await listener._handle_notification(_notification("synthetic-signature", 7))
        assert store.checkpoint("new-token:program") == (0, None)
        await asyncio.sleep(0.01)
        await listener._handle_notification(_notification("synthetic-signature", 7))

    asyncio.run(scenario())

    assert fetches == 2
    assert len(callbacks) == 1
    assert store.checkpoint("new-token:program") == (1, "synthetic-signature")
    assert store.metrics()["retried"] == 1
    metrics_text = (tmp_path / "metrics.jsonl").read_text(encoding="utf-8")
    assert "synthetic-signature" not in metrics_text
    assert '"stage":"event.end_to_end"' in metrics_text
    assert '"stage":"event.decision"' in metrics_text
    assert '"stage":"event.parse"' in metrics_text
    summary = summarize_metrics(tmp_path / "metrics.jsonl")
    end_to_end = summary["stages"]["event.end_to_end:helius"]
    assert end_to_end["count"] == 1
    assert end_to_end["p50_ms"] == end_to_end["p95_ms"] == end_to_end["p99_ms"]


def test_protection_ws_event_is_persisted_before_parse_and_retried(
    monkeypatch, tmp_path
):
    store = HeliusEventStore(
        tmp_path / "events.sqlite3", retry_base_seconds=0.001, retry_max_seconds=0.01
    )
    scanner = ProtectionScanner(
        SimpleNamespace(state={"protection_targets": {}}), event_store=store
    )
    scanner._sub_id_to_address[9] = "wallet-public"
    fetches = 0

    async def fetch(_signature):
        nonlocal fetches
        fetches += 1
        return None if fetches == 1 else {"nativeTransfers": []}

    monkeypatch.setattr(protection_scanner.rpc_client, "fetch_parsed_transaction", fetch)

    async def scenario():
        await scanner._handle_message(_notification("synthetic-signature", 9))
        assert store.checkpoint("protection:wallet-public") == (0, None)
        await asyncio.sleep(0.01)
        await scanner._handle_message(_notification("synthetic-signature", 9))

    asyncio.run(scenario())

    assert fetches == 2
    assert store.checkpoint("protection:wallet-public") == (
        1,
        "synthetic-signature",
    )
    assert store.metrics()["retried"] == 1


def test_malformed_ws_notifications_mark_active_streams_for_replay(tmp_path):
    bad_notification = json.dumps({"params": {"result": {"value": {}}}})
    copy_store = HeliusEventStore(tmp_path / "copy.sqlite3")
    copytrade = CopyTradeListener(
        SimpleNamespace(state={}), None, None, event_store=copy_store
    )
    copytrade._subscribed_wallets = {"copy-wallet"}

    protection_store = HeliusEventStore(tmp_path / "protection.sqlite3")
    protection = ProtectionScanner(
        SimpleNamespace(state={"protection_targets": {}}),
        event_store=protection_store,
    )
    protection._subscribed_addresses = {"protection-wallet"}

    token_store = HeliusEventStore(tmp_path / "token.sqlite3")
    token = NewTokenListener(lambda _event: None, event_store=token_store)
    token._per_wallet_mode = True
    token._subscribed_wallets = {"token-wallet"}

    async def scenario():
        await copytrade._handle_message(bad_notification)
        await copytrade._handle_message(json.dumps({"id": 1, "error": {"code": -1}}))
        await protection._handle_message(bad_notification)
        await protection._handle_message(json.dumps({"id": 1, "error": {"code": -1}}))
        await token._handle_notification(bad_notification)
        await token._handle_notification(json.dumps({"id": 1, "error": {"code": -1}}))

    asyncio.run(scenario())

    assert copy_store.recovery_required_streams("copytrade:") == [
        "copytrade:copy-wallet"
    ]
    assert protection_store.recovery_required_streams("protection:") == [
        "protection:protection-wallet"
    ]
    assert token_store.recovery_required_streams("new-token:") == [
        "new-token:token-wallet"
    ]
