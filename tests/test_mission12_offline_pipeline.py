from __future__ import annotations

import asyncio
import json
import time

import pytest

import config
import execution_metrics
import helius_event_store
import main
import paper_trader
import wallet_history
from copytrade_listener import CopyTradeListener
from helius_event_store import (
    EventIntakePausedError,
    EventRecoveryError,
    HeliusEventStore,
    recover_required_streams,
)
from paper_trader import PaperTrader
from websocket_listener import NewTokenListener


COPY_WALLET = "synthetic-copy-wallet"
DEV_WALLET = "synthetic-dev-wallet"
TOKEN_MINT = "synthetic-token-mint"


def _security_result(*, honeypot: bool = False) -> dict:
    return {
        "error": None,
        "risk_model": "goplus_solana_authorities_v1",
        "security_score": 80,
        "is_honeypot": True if honeypot else None,
        "honeypot_detection_available": False,
        "buy_tax": None,
        "sell_tax": None,
        "is_mintable": False,
        "is_freezable": False,
        "is_closable": False,
        "metadata_mutable": False,
        "non_transferable": False,
        "default_account_state": 1,
        "transfer_fee_bps": 0,
        "transfer_hook_present": False,
        "max_dex_tvl_usd": 25_000.0,
        "lp_lock_data_available": True,
        "lp_locked_pct": 100.0,
    }


class _Store:
    def __init__(self, *, mode: str = "track_buy", emergency_stop: bool = False):
        settings = dict(config.DEFAULT_WALLET_SETTINGS)
        settings.update(
            {
                "buy_amount_sol": 0.1,
                "buy_only_once": True,
                "buy_mode": "simple",
                "max_market_cap": None,
                "max_token_age_at_buy_s": 0,
                "max_token_age_min": 0,
                "security_check_enabled": True,
                "min_security_score": 60,
                "pullback_entry_enabled": False,
            }
        )
        self.state = {
            "emergency_stop": emergency_stop,
            "emergency_stop_reason": "synthetic test state",
            "session_losses_usd": 0.0,
            "session_losses_reset_at": time.time(),
            "open_positions": [],
            "closed_positions": [],
            "trades": [],
            "monitored_dev_wallets": {
                COPY_WALLET: {
                    "mode": mode,
                    "label": "synthetic wallet",
                    "backtest_ratio": 1.0,
                    "settings": settings,
                },
                DEV_WALLET: {
                    "mode": "track_creation",
                    "label": "synthetic developer",
                    "backtest_ratio": 1.0,
                    "settings": settings,
                },
            },
        }
        self._settings = settings
        self._bought: set[tuple[str, str]] = set()

    def get_wallet_settings(self, _wallet: str) -> dict:
        return self._settings

    def is_auto_buy_active(self, _wallet: str) -> bool:
        return True

    def has_already_bought(self, wallet: str, mint: str) -> bool:
        return (wallet, mint) in self._bought

    def mark_bought(self, wallet: str, mint: str) -> None:
        self._bought.add((wallet, mint))

    def save(self) -> None:
        return None

    def record_trade_result(self, _wallet: str, *, is_win: bool) -> bool:
        return False

    def update_wallet_activity(self, _wallet: str) -> None:
        return None

    def check_and_update_follow_cooldown(self, _wallet: str) -> bool:
        return True

    def is_dev_blocked(self, _wallet: str, _creator: str) -> bool:
        return False

    def is_dev_monitored(self, wallet: str) -> bool:
        return wallet in self.state["monitored_dev_wallets"]


class _RecordingRugScanner:
    def __init__(self) -> None:
        self.candidates: list[tuple[str, str, int | None, str | None]] = []

    def register_candidate(self, mint, developer, *, created_at, created_at_source):
        self.candidates.append((mint, developer, created_at, created_at_source))


def _copy_buy_transaction() -> dict:
    return {
        "tokenTransfers": [
            {
                "mint": TOKEN_MINT,
                "fromUserAccount": "synthetic-pool",
                "toUserAccount": COPY_WALLET,
            }
        ],
        "nativeTransfers": [
            {
                "fromUserAccount": COPY_WALLET,
                "toUserAccount": "synthetic-pool",
                "amount": 1_000_000_000,
            }
        ],
    }


def _configure_paper(monkeypatch, security_result: dict) -> None:
    async def pair_data(_mint: str) -> dict:
        return {"priceUsd": "1", "marketCap": "1000"}

    async def no_bonding_price(_mint: str):
        return None

    async def sol_usd_rate() -> float:
        return 1.0

    async def token_creator(_mint: str) -> str:
        return "synthetic-other-creator"

    async def security_check(_mint: str) -> dict:
        return security_result

    monkeypatch.setattr(config, "TRADING_MODE", "PAPER")
    monkeypatch.setattr(paper_trader, "_get_pair_data", pair_data)
    monkeypatch.setattr(paper_trader, "get_bonding_curve_price", no_bonding_price)
    monkeypatch.setattr(paper_trader, "get_sol_usd_rate", sol_usd_rate)
    monkeypatch.setattr(wallet_history, "get_token_creator", token_creator)
    monkeypatch.setattr(
        paper_trader.security_check, "check_token_security", security_check
    )


def _bot_with_store(store: _Store) -> main.SniperBot:
    bot = object.__new__(main.SniperBot)
    bot.data_store = store
    bot.trader = PaperTrader(store)
    bot.notifier = None
    bot.rug_scanner = _RecordingRugScanner()
    return bot


def _notification(signature: str, subscription: int, slot: int = 42) -> str:
    return json.dumps(
        {
            "method": "logsNotification",
            "params": {
                "subscription": subscription,
                "result": {
                    "context": {"slot": slot},
                    "value": {
                        "signature": signature,
                        "logs": ["Program log: Instruction: Buy"],
                    },
                },
            },
        }
    )


def _creation_notification(signature: str, subscription: int, slot: int = 42) -> str:
    return json.dumps(
        {
            "method": "logsNotification",
            "params": {
                "subscription": subscription,
                "result": {
                    "context": {"slot": slot},
                    "value": {
                        "signature": signature,
                        "logs": ["Program log: Instruction: Create"],
                    },
                },
            },
        }
    )


def test_copy_buy_reaches_paper_risk_gate_once_with_synthetic_metrics(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    store = _Store()
    bot = _bot_with_store(store)
    _configure_paper(monkeypatch, _security_result())

    listener = CopyTradeListener(
        store,
        on_buy=bot.on_copytrade_buy,
        on_sell=bot.on_copytrade_sell,
        event_store=HeliusEventStore(tmp_path / "copy.sqlite3"),
    )
    listener._subscribed_wallets = {COPY_WALLET}
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()
    fetch_calls = 0

    async def parsed_transaction(_signature: str) -> dict:
        nonlocal fetch_calls
        fetch_calls += 1
        fetch_started.set()
        await release_fetch.wait()
        return _copy_buy_transaction()

    listener._fetch_parsed_transaction = parsed_transaction

    async def scenario() -> None:
        first = asyncio.create_task(listener._process_transaction(
            "synthetic-copy-signature",
            True,
            source=f"copytrade:{COPY_WALLET}",
            slot=42,
        ))
        await fetch_started.wait()
        await listener._process_transaction(
            "synthetic-copy-signature",
            True,
            source=f"copytrade:{COPY_WALLET}",
            slot=42,
        )
        release_fetch.set()
        await first
        await listener._process_transaction(
            "synthetic-copy-signature",
            True,
            source=f"copytrade:{COPY_WALLET}",
            slot=42,
        )

    asyncio.run(scenario())

    assert len(store.state["open_positions"]) == 1
    assert store.state["open_positions"][0]["token_mint"] == TOKEN_MINT
    assert store.state["open_positions"][0]["reason"].startswith("Copy trade")
    assert listener._event_store.checkpoint(f"copytrade:{COPY_WALLET}") == (
        1,
        "synthetic-copy-signature",
    )
    # One concurrent delivery and one later redelivery are both rejected.
    assert listener._event_store.metrics()["duplicates"] == 2
    assert fetch_calls == 1

    metrics_path = tmp_path / "metrics.jsonl"
    metrics_text = metrics_path.read_text(encoding="utf-8")
    assert "synthetic-copy-signature" not in metrics_text
    assert COPY_WALLET not in metrics_text
    assert TOKEN_MINT not in metrics_text
    summary = execution_metrics.summarize_metrics(metrics_path)
    assert summary["stages"]["event.end_to_end:helius"]["count"] == 1
    assert summary["stages"]["decision.copytrade_buy:internal"]["count"] == 1


def test_tracked_developer_sniping_path_rejects_synthetic_honeypot(
    monkeypatch, tmp_path
):
    store = _Store(mode="track_creation")
    bot = _bot_with_store(store)
    _configure_paper(monkeypatch, _security_result(honeypot=True))

    event_store = HeliusEventStore(tmp_path / "new-token.sqlite3")
    listener = NewTokenListener(bot.on_new_token, event_store=event_store)
    listener._sub_id_to_stream[9] = "new-token:program"
    listener._extract_creation_info = lambda _parsed: (TOKEN_MINT, DEV_WALLET)

    async def parsed_transaction(_signature: str) -> dict:
        return {"slot": 42, "timestamp": 1_700_000_000}

    listener._fetch_parsed_transaction = parsed_transaction

    asyncio.run(
        listener._handle_notification(
            _creation_notification("synthetic-snipe-signature", 9)
        )
    )

    assert store.state["open_positions"] == []
    assert bot.rug_scanner.candidates == [
        (
            TOKEN_MINT,
            DEV_WALLET,
            1_700_000_000,
            "helius_enhanced_transaction.timestamp",
        )
    ]
    assert event_store.checkpoint("new-token:program") == (
        1,
        "synthetic-snipe-signature",
    )


def test_emergency_stop_blocks_copy_buy_before_paper_simulation(
    monkeypatch, tmp_path
):
    store = _Store(emergency_stop=True)
    bot = _bot_with_store(store)
    _configure_paper(monkeypatch, _security_result())
    listener = CopyTradeListener(
        store,
        on_buy=bot.on_copytrade_buy,
        on_sell=bot.on_copytrade_sell,
        event_store=HeliusEventStore(tmp_path / "stopped.sqlite3"),
    )
    listener._subscribed_wallets = {COPY_WALLET}

    async def parsed_transaction(_signature: str) -> dict:
        return _copy_buy_transaction()

    listener._fetch_parsed_transaction = parsed_transaction

    asyncio.run(
        listener._process_transaction(
            "synthetic-stopped-signature",
            True,
            source=f"copytrade:{COPY_WALLET}",
            slot=42,
        )
    )

    assert store.state["emergency_stop"] is True
    assert store.state["open_positions"] == []
    assert listener._event_store.checkpoint(f"copytrade:{COPY_WALLET}") == (
        1,
        "synthetic-stopped-signature",
    )


def test_unavailable_goplus_source_fails_closed_before_paper_copy_buy(
    monkeypatch, tmp_path
):
    store = _Store()
    bot = _bot_with_store(store)
    _configure_paper(monkeypatch, {"error": "synthetic provider unavailable"})
    listener = CopyTradeListener(
        store,
        on_buy=bot.on_copytrade_buy,
        on_sell=bot.on_copytrade_sell,
        event_store=HeliusEventStore(tmp_path / "unavailable-security.sqlite3"),
    )
    listener._subscribed_wallets = {COPY_WALLET}

    async def parsed_transaction(_signature: str) -> dict:
        return _copy_buy_transaction()

    listener._fetch_parsed_transaction = parsed_transaction

    asyncio.run(
        listener._process_transaction(
            "synthetic-security-error-signature",
            True,
            source=f"copytrade:{COPY_WALLET}",
            slot=42,
        )
    )

    assert store.state["open_positions"] == []
    assert listener._event_store.checkpoint(f"copytrade:{COPY_WALLET}") == (
        1,
        "synthetic-security-error-signature",
    )


def test_copy_sell_uses_durable_signal_once_for_paper_close(monkeypatch, tmp_path):
    store = _Store(mode="track_sell")
    position = {
        "token_mint": TOKEN_MINT,
        "source_wallet": COPY_WALLET,
        "entry_price": 1.0,
        "entry_time": time.time() - 60,
        "cost_basis_usd": 1.0,
        "units": 1.0,
        "settings": store._settings,
        "total_sol_received": 0.0,
        "total_sol_invested": 1.0,
        "simulated_fees_sol": 0.0,
        "simulated_fees_usd": 0.0,
        "fee_lamports": 0,
        "fee_accounting_version": 1,
        "fee_accounting_complete": True,
    }
    store.state["open_positions"].append(position)
    bot = _bot_with_store(store)
    _configure_paper(monkeypatch, _security_result())

    async def pair_data(_mint: str) -> dict:
        return {"priceUsd": "2"}

    monkeypatch.setattr(main.backtest, "_get_pair_data", pair_data)
    listener = CopyTradeListener(
        store,
        on_buy=None,
        on_sell=bot.on_copytrade_sell,
        event_store=HeliusEventStore(tmp_path / "copy-sell.sqlite3"),
    )
    listener._subscribed_wallets = {COPY_WALLET}

    async def parsed_transaction(_signature: str) -> dict:
        return {
            "tokenTransfers": [
                {
                    "mint": TOKEN_MINT,
                    "fromUserAccount": COPY_WALLET,
                    "toUserAccount": "synthetic-pool",
                }
            ],
            "nativeTransfers": [
                {
                    "fromUserAccount": "synthetic-pool",
                    "toUserAccount": COPY_WALLET,
                    "amount": 1_000_000_000,
                }
            ],
        }

    listener._fetch_parsed_transaction = parsed_transaction

    async def scenario() -> None:
        await listener._process_transaction(
            "synthetic-copy-sell-signature",
            True,
            source=f"copytrade:{COPY_WALLET}",
            slot=43,
        )
        await listener._process_transaction(
            "synthetic-copy-sell-signature",
            True,
            source=f"copytrade:{COPY_WALLET}",
            slot=43,
        )

    asyncio.run(scenario())

    assert store.state["open_positions"] == []
    assert store.state["closed_positions"] == [position]
    assert position["close_reason"] == "COPY_SELL"
    assert position["exit_price"] == 2.0
    assert listener._event_store.checkpoint(f"copytrade:{COPY_WALLET}") == (
        1,
        "synthetic-copy-sell-signature",
    )
    assert listener._event_store.metrics()["duplicates"] == 1


@pytest.mark.parametrize(
    "source_error",
    [TimeoutError("synthetic timeout"), RuntimeError("synthetic HTTP 429")],
)
def test_history_replay_source_failure_keeps_pause_until_successful_recovery(
    monkeypatch, tmp_path, source_error
):
    store = HeliusEventStore(tmp_path / "replay.sqlite3")
    stream = "copytrade:synthetic-wallet"
    store.ensure_stream(stream)
    store.enqueue(stream, "synthetic-checkpoint", {}, slot=10)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    store.mark_processed(checkpoint.event_id)
    store.mark_recovery_required(stream, "connection_gap")

    calls = 0
    processed: list[str] = []

    async def fetch_page(_address: str, _before: str | None, _limit: int):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise source_error
        return [
            {"signature": "synthetic-newer", "slot": 12, "transactionIndex": 1},
            {"signature": "synthetic-checkpoint", "slot": 10, "transactionIndex": 0},
        ]

    async def handler(event) -> None:
        processed.append(event.signature)

    monkeypatch.setattr(helius_event_store, "fetch_signature_page", fetch_page)

    async def scenario() -> None:
        ready = await recover_required_streams(
            store,
            "copytrade:",
            handler,
            lambda _stream: "synthetic-wallet",
        )
        assert ready is False
        assert store.checkpoint(stream) == (1, "synthetic-checkpoint")
        assert store.intake_paused(stream) is True

        with pytest.raises(EventIntakePausedError):
            store.enqueue(stream, "synthetic-buffered-live-frame", {})

        ready = await recover_required_streams(
            store,
            "copytrade:",
            handler,
            lambda _stream: "synthetic-wallet",
        )
        assert ready is True

    asyncio.run(scenario())

    assert calls == 2
    assert processed == ["synthetic-newer"]
    assert store.checkpoint(stream) == (2, "synthetic-newer")
    assert store.intake_paused(stream) is False
    assert store.recovery_required_streams("copytrade:") == []
    assert store.metrics()["paused_intake_rejections"] == 1


def test_replay_error_is_sanitized_and_does_not_advance_checkpoint(tmp_path):
    store = HeliusEventStore(tmp_path / "replay-error.sqlite3")
    stream = "copytrade:synthetic-wallet"
    store.ensure_stream(stream)
    store.enqueue(stream, "synthetic-checkpoint", {}, slot=10)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    store.mark_processed(checkpoint.event_id)
    store.mark_recovery_required(stream, "connection_gap")

    async def unavailable(_address: str, _before: str | None, _limit: int):
        raise TimeoutError("credential-bearing detail must not be propagated")

    with pytest.raises(EventRecoveryError, match="temporarily unavailable") as exc_info:
        asyncio.run(
            helius_event_store.replay_stream_history(
                store, stream, "synthetic-wallet", unavailable
            )
        )

    assert "credential-bearing" not in str(exc_info.value)
    assert store.checkpoint(stream) == (1, "synthetic-checkpoint")
    assert store.intake_paused(stream) is True
