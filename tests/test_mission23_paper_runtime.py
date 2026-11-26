"""Offline integration coverage for the Mission 23 PAPER admission route."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import multiprocessing
import sqlite3
import time
from types import SimpleNamespace

import config
from helius_event_store import EventRecord, HeliusEventStore
from mission13_admission import PaperCandidateRequest
from mission13_market_policy import (
    MarketRegime,
    RegimeObservation,
    RegimePolicy,
    StrategyCandidate,
)
from mission13_scoring import ScoringProfile, SignalObservation, SignalRule
from mission18_paper_admission import SQLitePaperAdmissionStore
from mission23_paper_runtime import Mission23PaperRuntime
from monitoring_list import DataStore
from paper_trader import PaperTrader


def _request(as_of: datetime) -> PaperCandidateRequest:
    observed = as_of
    observations = (
        SignalObservation("liquidity_usd", 80, observed, "SIMULATED_fixture", "SIMULATED fixture"),
        SignalObservation("holder_concentration_pct", 15, observed, "SIMULATED_fixture", "SIMULATED fixture"),
        SignalObservation("mint_authority_active", False, observed, "SIMULATED_fixture", "SIMULATED fixture"),
    )
    market = tuple(
        RegimeObservation(as_of - timedelta(minutes=offset), close, "SIMULATED_fixture")
        for offset, close in ((2, 100), (1, 102), (0, 104))
    )
    return PaperCandidateRequest(
        signal_id="fixture-signal",
        correlation_key="fixture-correlation",
        observations=observations,
        scoring_profile=ScoringProfile(
            profile_id="mission23-synthetic-unvalidated-v1",
            rules=(
                SignalRule("liquidity_usd", 2, 0, 100, timedelta(minutes=2)),
                SignalRule("holder_concentration_pct", 1, 0, 100, timedelta(minutes=2), False),
                SignalRule(
                    "mint_authority_active", 1, 0, 1, timedelta(minutes=2),
                    False, True, block_above=0, block_reason_code="mint_authority_active",
                ),
            ),
            minimum_score=60,
        ),
        regime_observations=market,
        regime_policy=RegimePolicy(
            min_observations=3,
            expected_interval=timedelta(minutes=1),
            max_latest_age=timedelta(seconds=30),
            max_observation_gap=timedelta(minutes=1),
            trend_threshold_pct=1,
            high_volatility_threshold_pct=5,
        ),
        strategies=(
            StrategyCandidate(
                "SIMULATED_copytrade", "copy_trading_existing",
                frozenset({MarketRegime.BULLISH_TREND}),
                frozenset({"liquidity_usd", "holder_concentration_pct", "mint_authority_active"}),
            ),
        ),
    )


def _event(*, event_id: int = 7, stream: str = "simulated:copytrade") -> EventRecord:
    return EventRecord(
        event_id=event_id,
        stream=stream,
        sequence=1,
        signature=f"SIMULATED-signature-{event_id}",
        correlation_id=f"SIMULATED-correlation-{event_id}",
        slot=123,
        payload={
            "provenance_state": "SIMULATED",
            "finality_state": "SIMULATED",
            "completeness_state": "SIMULATED",
        },
        attempts=0,
        received_at=time.time(),
        transaction_index=0,
    )


class _ConcurrentDataStore:
    def __init__(self, state):
        self.state = state

    def get_wallet_settings(self, _wallet):
        return {"buy_only_once": False}


class _ConcurrentTrader:
    require_mission23_admission = True

    def __init__(self, store, state):
        self._store = store
        self.data_store = _ConcurrentDataStore(state)

    async def open_position(
        self, token_mint, source_wallet, _reason, *,
        mission18_admission_store, mission18_signal_id, provenance_state,
        **_kwargs,
    ):
        snapshot = mission18_admission_store.snapshot()
        state_after = deepcopy(snapshot.state)
        position = {
            "mission18_signal_id": mission18_signal_id,
            "token_mint": token_mint,
            "source_wallet": source_wallet,
            "cost_basis_usd": 1.0,
            "provenance_state": provenance_state,
            "accounting_eligible": False,
        }
        state_after.setdefault("open_positions", []).append(position)
        mission18_admission_store.commit_position(
            mission18_signal_id, state_after, expected_revision=snapshot.revision,
        )
        return position


def _concurrent_runtime_worker(db_path, barrier, result_queue):
    import config as worker_config

    worker_config.TRADING_MODE = "PAPER"
    worker_config.LIVE_EXECUTION_ENABLED = False
    admission = SQLitePaperAdmissionStore(db_path)
    snapshot = admission.snapshot()
    runtime = Mission23PaperRuntime(admission, _ConcurrentTrader(admission, snapshot.state))
    barrier.wait(timeout=20)
    result = asyncio.run(runtime.process_event(
        _event(), _request(datetime.now(timezone.utc)),
        source_wallet="SIMULATED-wallet", token_mint="SIMULATED-mint",
        reason="multiprocess fixture", proposed_exposure_usd=10,
    ))
    result_queue.put((result.status, result.reason))


def _runtime(tmp_path, monkeypatch):
    db_path = tmp_path / "bot-state.sqlite3"
    monkeypatch.setattr(config, "DATA_BACKEND", "sqlite")
    monkeypatch.setattr(config, "SQLITE_DB_FILE", str(db_path))
    monkeypatch.setattr(config, "DATA_FILE", str(tmp_path / "legacy.json"))
    monkeypatch.setattr(config, "TRADING_MODE", "PAPER")
    monkeypatch.setattr(config, "LIVE_EXECUTION_ENABLED", False)
    data_store = DataStore()
    data_store.state["emergency_stop"] = False
    data_store.state["session_losses_usd"] = 0.0
    data_store.state["session_losses_reset_at"] = time.time()
    data_store.state["monitored_dev_wallets"]["SIMULATED-wallet"] = {
        "label": "SIMULATED",
        "mode": "track_buy",
        "settings": {
            **config.DEFAULT_WALLET_SETTINGS,
            "buy_amount_sol": 0.01,
            "buy_mode": "simple",
            "security_check_enabled": False,
            "buy_only_once": True,
            "max_token_age_at_buy_s": None,
        },
        "bought_tokens": [],
    }
    data_store.save()
    admission_store = SQLitePaperAdmissionStore(db_path)
    trader = PaperTrader(data_store)
    trader.require_mission23_admission = True

    async def allow_process_risk(*_args, **_kwargs):
        return True, None

    trader._reserve_entry_risk = allow_process_risk
    data_store.is_auto_buy_active = lambda _wallet: True
    data_store.has_already_bought = lambda _wallet, _mint: False
    runtime = Mission23PaperRuntime(admission_store, trader)
    return runtime, admission_store, data_store, trader


def test_copytrade_callback_returns_unknown_disposition_to_durable_listener(monkeypatch):
    import main
    from mission23_paper_runtime import PaperRuntimeResult

    monkeypatch.setattr(config, "TRADING_MODE", "PAPER")

    class Runtime:
        async def process_event(self, *_args, **_kwargs):
            return PaperRuntimeResult("unknown", "live_signal_provenance_unverified")

    bot = main.SniperBot.__new__(main.SniperBot)
    bot.paper_runtime = Runtime()
    result = asyncio.run(bot.on_copytrade_buy(
        "SIMULATED-wallet", "SIMULATED-mint", "SIMULATED-signature",
        event_record=_event(stream="copytrade:wallet"),
    ))

    assert result.status == "unknown"
    assert result.reason == "live_signal_provenance_unverified"


def test_real_provider_event_stays_blocked_without_finality_and_provenance(tmp_path, monkeypatch):
    runtime, admission, _data_store, _trader = _runtime(tmp_path, monkeypatch)
    result = asyncio.run(runtime.process_event(
        _event(stream="copytrade:tracked"), _request(datetime.now(timezone.utc)),
        source_wallet="SIMULATED-wallet", token_mint="SIMULATED-mint",
        reason="fixture", proposed_exposure_usd=10,
    ))

    assert result.status == "unknown"
    assert result.reason == "live_signal_provenance_unverified"
    assert admission.counts()["pending"] == 0


def test_missing_mission13_evidence_is_blocked_without_reservation(tmp_path, monkeypatch):
    runtime, admission, _data_store, _trader = _runtime(tmp_path, monkeypatch)
    result = asyncio.run(runtime.process_event(
        _event(), None, source_wallet="SIMULATED-wallet", token_mint="SIMULATED-mint",
        reason="fixture", proposed_exposure_usd=10,
    ))

    assert result.status == "blocked"
    assert result.reason == "mission13_evidence_required"
    assert admission.counts()["pending"] == 0


def test_direct_paper_trader_entry_without_mission23_token_is_refused(tmp_path, monkeypatch):
    runtime, admission, data_store, trader = _runtime(tmp_path, monkeypatch)

    async def unexpected_provider_call(_mint):
        raise AssertionError("unadmitted PAPER entry must stop before price/provider work")

    import paper_trader
    monkeypatch.setattr(paper_trader, "get_bonding_curve_price", unexpected_provider_call)
    position = asyncio.run(trader.open_position(
        "SIMULATED-mint", "SIMULATED-wallet", "legacy direct call",
    ))

    assert position is None
    assert data_store.state["open_positions"] == []
    assert admission.counts()["pending"] == 0


def test_buy_the_dip_is_blocked_without_incremental_durable_reservation(tmp_path, monkeypatch):
    import paper_trader

    _runtime_obj, _admission, _data_store, trader = _runtime(tmp_path, monkeypatch)
    position = {
        "token_mint": "SIMULATED-mint",
        "source_wallet": "SIMULATED-wallet",
        "units": 1.0,
        "cost_basis_usd": 10.0,
        "ath_market_cap": 100.0,
        "settings": {"buy_the_dip_levels": [{"drop_pct": 10, "amount_sol": 0.1}]},
        "dip_levels_hit": [],
    }

    async def unexpected_market_read(_mint):
        raise AssertionError("blocked DCA must stop before quote reads or buys")

    monkeypatch.setattr(paper_trader, "_get_pair_data", unexpected_market_read)
    asyncio.run(trader._check_buy_the_dip(position, 1.0))

    assert position["units"] == 1.0
    assert position["cost_basis_usd"] == 10.0
    assert position["dip_levels_hit"] == []


def test_emergency_stop_blocks_even_complete_synthetic_fixture(tmp_path, monkeypatch):
    runtime, admission, data_store, _trader = _runtime(tmp_path, monkeypatch)
    data_store.state["emergency_stop"] = True
    data_store.save()
    result = asyncio.run(runtime.process_event(
        _event(), _request(datetime.now(timezone.utc)),
        source_wallet="SIMULATED-wallet", token_mint="SIMULATED-mint",
        reason="fixture", proposed_exposure_usd=10,
    ))

    assert result.status == "blocked"
    assert result.reason == "emergency_stop_active"
    assert admission.counts()["pending"] == 0


def test_invalid_event_identity_and_order_are_refused(tmp_path, monkeypatch):
    runtime, admission, _data_store, _trader = _runtime(tmp_path, monkeypatch)
    request = _request(datetime.now(timezone.utc))
    malformed_identity = asyncio.run(runtime.process_event(
        replace(_event(), event_id=True), request,
        source_wallet="SIMULATED-wallet", token_mint="SIMULATED-mint",
        reason="fixture", proposed_exposure_usd=10,
    ))
    malformed_order = asyncio.run(runtime.process_event(
        replace(_event(), transaction_index=-1), request,
        source_wallet="SIMULATED-wallet", token_mint="SIMULATED-mint",
        reason="fixture", proposed_exposure_usd=10,
    ))

    assert malformed_identity.reason == "durable_event_identity_invalid"
    assert malformed_order.reason == "canonical_event_order_required"
    assert admission.counts()["pending"] == 0


def test_two_processes_cannot_create_two_positions_from_one_event(tmp_path, monkeypatch):
    _runtime_obj, admission, _data_store, _trader = _runtime(tmp_path, monkeypatch)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_concurrent_runtime_worker,
            args=(str(admission.path), barrier, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
    assert all(process.exitcode == 0 for process in processes)
    outcomes = [results.get(timeout=5) for _ in processes]

    assert sorted(status for status, _reason in outcomes) == ["committed", "unknown"]
    assert admission.counts()["committed"] == 1
    assert admission.counts()["pending"] == 0
    assert len(admission.snapshot().state["open_positions"]) == 1


def test_duplicate_active_reservation_is_unknown_without_mutating_first_worker(
    tmp_path, monkeypatch,
):
    runtime, admission, _data_store, _trader = _runtime(tmp_path, monkeypatch)
    signal_id = "helius-event:7"
    correlation_key = "helius-correlation:SIMULATED-correlation-7"
    reserved, reason = admission.reserve_entry(
        signal_id,
        correlation_key,
        10,
        max_exposure_usd=config.MAX_OPEN_EXPOSURE_USD,
        max_open_positions=config.MAX_OPEN_POSITIONS,
        max_session_loss_usd=config.CIRCUIT_BREAKER_USD,
    )
    assert reserved is True, reason

    result = asyncio.run(runtime.process_event(
        _event(), _request(datetime.now(timezone.utc)),
        source_wallet="SIMULATED-wallet", token_mint="SIMULATED-mint",
        reason="duplicate worker fixture", proposed_exposure_usd=10,
    ))

    assert result.status == "unknown"
    assert result.reason == "durable_reservation_outcome_unresolved"
    assert admission.counts()["pending"] == 1
    assert admission.counts()["unknown"] == 0
    assert admission.counts()["committed"] == 0


def test_uncertain_paper_simulation_keeps_unknown_reservation_after_restart(tmp_path, monkeypatch):
    runtime, admission, _data_store, trader = _runtime(tmp_path, monkeypatch)

    async def ambiguous_simulation(*_args, **_kwargs):
        raise RuntimeError("synthetic interruption after reservation")

    monkeypatch.setattr(trader, "open_position", ambiguous_simulation)
    result = asyncio.run(runtime.process_event(
        _event(), _request(datetime.now(timezone.utc)),
        source_wallet="SIMULATED-wallet", token_mint="SIMULATED-mint",
        reason="interrupted fixture", proposed_exposure_usd=10,
    ))

    assert result.status == "unknown"
    assert admission.counts()["unknown"] == 1
    restarted = SQLitePaperAdmissionStore(tmp_path / "bot-state.sqlite3")
    assert restarted.snapshot().reserved_positions == 1
    assert restarted.counts()["unknown"] == 1


def test_synthetic_flow_uses_m13_m18_and_atomically_persists_unverified_position(
    tmp_path, monkeypatch,
):
    import paper_trader

    runtime, admission, data_store, trader = _runtime(tmp_path, monkeypatch)
    inbox_path = tmp_path / "helius-inbox.sqlite3"
    event_store = HeliusEventStore(inbox_path)
    stream = "simulated:copytrade"
    queued = event_store.enqueue(
        stream,
        "SIMULATED-signature-e2e-1",
        {
            "provenance_state": "SIMULATED",
            "finality_state": "SIMULATED",
            "completeness_state": "SIMULATED",
        },
        slot=123,
        transaction_index=0,
        is_replay=True,
    )
    assert queued.inserted is True
    claimed = event_store.claim_next(stream)
    assert claimed is not None
    assert claimed.event_id == queued.event_id
    assert claimed.slot == 123 and claimed.transaction_index == 0
    assert claimed.payload["provenance_state"] == "SIMULATED"
    event_store.mark_effect_started(
        claimed.event_id, claim_generation=claimed.claim_generation,
    )
    monkeypatch.setattr(paper_trader, "get_bonding_curve_price", lambda _mint: asyncio.sleep(0, result=None))
    monkeypatch.setattr(
        paper_trader,
        "_get_pair_data",
        lambda _mint: asyncio.sleep(0, result={"priceUsd": "0.01", "marketCap": 5_000}),
    )
    monkeypatch.setattr(paper_trader, "get_sol_usd_rate", lambda: asyncio.sleep(0, result=100.0))
    monkeypatch.setattr(paper_trader, "_simulated_paper_fee_sol", lambda _mode: 0.0)

    async def execute_buy(_mint, price, _settings):
        return {
            "units": 100,
            "cost_basis_usd": 1.0,
            "actual_price": price,
            "signature": None,
            "sol_usd_rate": 100.0,
        }

    monkeypatch.setattr(trader, "_execute_buy", execute_buy)
    monkeypatch.setattr(trader, "_start_position_monitor", lambda *_a, **_k: False)
    before_pnl = data_store.state["total_pnl_usd"]
    request = replace(
        _request(datetime.now(timezone.utc)),
        signal_id=f"fixture-signal:{claimed.event_id}",
        correlation_key=claimed.correlation_id,
    )
    result = asyncio.run(runtime.process_event(
        claimed, request, source_wallet="SIMULATED-wallet", token_mint="SIMULATED-mint",
        reason="offline fixture", proposed_exposure_usd=10,
    ))

    assert result.status == "committed", result
    event_store.mark_processed(
        claimed.event_id, claim_generation=claimed.claim_generation,
    )
    assert result.position_opened is True
    assert result.provenance_state == "SIMULATED"
    assert admission.counts()["committed"] == 1
    position = data_store.state["open_positions"][-1]
    assert position["provenance_state"] == "SIMULATED"
    assert position["accounting_eligible"] is False
    assert position["mission18_signal_id"] == f"helius-event:{claimed.event_id}"
    assert data_store.state["total_pnl_usd"] == before_pnl

    # Both durable stores are reopened as a simulated process restart. The inbox
    # must show ACK only after the M18 commit, and cannot claim the event again.
    restarted_event_store = HeliusEventStore(inbox_path)
    with sqlite3.connect(inbox_path) as inbox_db:
        row = inbox_db.execute(
            "SELECT status, effect_started FROM event_inbox WHERE event_id=?",
            (claimed.event_id,),
        ).fetchone()
    assert row == ("processed", 1)
    assert restarted_event_store.pending_count(stream) == 0
    assert restarted_event_store.claim_next(stream) is None
    duplicate_enqueue = restarted_event_store.enqueue(
        stream,
        "SIMULATED-signature-e2e-1",
        {
            "provenance_state": "SIMULATED",
            "finality_state": "SIMULATED",
            "completeness_state": "SIMULATED",
        },
        slot=123,
        transaction_index=0,
        is_replay=True,
    )
    assert duplicate_enqueue.inserted is False
    assert duplicate_enqueue.event_id == claimed.event_id
    restarted = SQLitePaperAdmissionStore(tmp_path / "bot-state.sqlite3")
    restarted_data_store = DataStore()
    assert len(restarted_data_store.state["open_positions"]) == 1
    assert restarted.counts()["committed"] == 1
    restarted_trader = PaperTrader(restarted_data_store)
    restarted_trader.require_mission23_admission = True
    restarted_runtime = Mission23PaperRuntime(restarted, restarted_trader)
    duplicate = asyncio.run(restarted_runtime.process_event(
        _event(), _request(datetime.now(timezone.utc)),
        source_wallet="SIMULATED-wallet", token_mint="SIMULATED-mint",
        reason="replay", proposed_exposure_usd=10,
    ))
    assert duplicate.status == "blocked"
    assert duplicate.reason == "buy_once_already_recorded"
    assert restarted.counts()["committed"] == 1
    assert len(data_store.state["open_positions"]) == 1

    other_event = asyncio.run(Mission23PaperRuntime(restarted, trader).process_event(
        _event(event_id=8), _request(datetime.now(timezone.utc)),
        source_wallet="SIMULATED-wallet", token_mint="SIMULATED-mint",
        reason="new signature for same token", proposed_exposure_usd=10,
    ))
    assert other_event.status == "blocked"
    assert other_event.reason == "buy_once_already_recorded"
    assert len(data_store.state["open_positions"]) == 1
