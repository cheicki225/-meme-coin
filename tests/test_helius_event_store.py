import asyncio
import multiprocessing
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from helius_event_store import (
    EventClaimLostError,
    EventQueueFullError,
    EventIntakePausedError,
    EventRecoveryError,
    EventStoreError,
    HeliusEventStore,
    RetryableEventError,
    drain_stream,
    process_next_event,
    recover_required_streams,
    replay_stream_history,
)
from execution_metrics import summarize_metrics


def _store(path, **kwargs):
    return HeliusEventStore(path, **kwargs)


async def _slot_zero():
    return 0


def _mock_zero_slots(monkeypatch):
    monkeypatch.setattr("helius_event_store.fetch_processed_slot", _slot_zero)
    monkeypatch.setattr("helius_event_store.fetch_finalized_slot", _slot_zero)


def _enqueue_ordered(store, stream, signature, payload=None, *, slot=10, index=0, now=None):
    """Insert fixture history with an explicit provider-reported order tuple."""
    return store.enqueue(
        stream,
        signature,
        payload or {},
        slot=slot,
        transaction_index=index,
        is_replay=True,
        now=now,
    )


def _mark_processed(store, event, *, now=None):
    return store.mark_processed(
        event.event_id,
        claim_generation=event.claim_generation,
        recovery_generation=event.recovery_generation,
        recovery=event.recovery_claim,
        now=now,
    )


def _mark_effect_started(store, event, *, now=None):
    return store.mark_effect_started(
        event.event_id,
        claim_generation=event.claim_generation,
        recovery_generation=event.recovery_generation,
        now=now,
    )


def _mark_unknown(store, event, error_code="handler_outcome_unknown"):
    return store.mark_unknown(
        event.event_id,
        error_code,
        claim_generation=event.claim_generation,
    )


def _enqueue_synthetic_event_in_process(path, barrier, result_queue, signature):
    store = HeliusEventStore(path)
    barrier.wait(timeout=20)
    result = store.enqueue(
        "fixture-stream", signature, {"synthetic": True}, slot=123
    )
    result_queue.put((result.inserted, result.status))


def _reconcile_unknown_in_process(path, barrier, result_queue, signature):
    store = HeliusEventStore(path)
    barrier.wait(timeout=20)
    result = store.enqueue(
        "fixture-stream",
        signature,
        {"canonical_order_evidence": {"trust": "provider_reported"}},
        slot=123,
        transaction_index=7,
        is_replay=True,
    )
    result_queue.put((result.inserted, result.status))


def test_future_schema_is_rejected_before_journal_mode_or_ddl_change(tmp_path):
    path = tmp_path / "future-schema.sqlite3"
    with sqlite3.connect(path) as database:
        database.execute("PRAGMA user_version=8")

    with pytest.raises(EventStoreError, match="schema is newer"):
        _store(path)

    with sqlite3.connect(path) as database:
        journal_mode = database.execute("PRAGMA journal_mode").fetchone()[0]
        version = database.execute("PRAGMA user_version").fetchone()[0]
        tables = database.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    assert journal_mode == "delete"
    assert version == 8
    assert tables == []


@pytest.fixture(autouse=True)
def _isolate_metrics_path(monkeypatch, tmp_path):
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(tmp_path / "metrics.jsonl"))


def test_event_is_persisted_before_processing_and_resumes_after_crash(tmp_path):
    path = tmp_path / "helius-events.sqlite3"
    store = _store(path, lease_seconds=2)
    inserted = store.enqueue(
        "copytrade:wallet-a", "signature-a", {"is_swap_related": True},
        slot=10, transaction_index=0, is_replay=True, now=100
    )
    assert inserted.inserted

    claimed_before_crash = store.claim_next("copytrade:wallet-a", now=100)
    assert claimed_before_crash is not None
    assert store.checkpoint("copytrade:wallet-a") == (0, None)

    restarted = _store(path, lease_seconds=2)
    assert restarted.claim_next("copytrade:wallet-a", now=101) is None
    recovered = restarted.claim_next("copytrade:wallet-a", now=103)

    assert recovered is not None
    assert recovered.signature == "signature-a"
    assert recovered.attempts == 2
    _mark_processed(restarted, recovered, now=104)
    assert restarted.checkpoint("copytrade:wallet-a") == (1, "signature-a")


def test_expired_claim_after_effect_boundary_becomes_unknown_on_restart(tmp_path):
    path = tmp_path / "events.sqlite3"
    store = _store(path, lease_seconds=2)
    store.enqueue("new-token:program", "signature-a", {}, now=100)
    claimed = store.claim_next("new-token:program", now=100)
    assert claimed is not None
    _mark_effect_started(store, claimed, now=100)

    restarted = _store(path, lease_seconds=2)
    assert restarted.claim_next("new-token:program", now=103) is None
    assert restarted.checkpoint("new-token:program") == (0, None)
    assert restarted.metrics()["unknown"] == 1
    assert restarted.intake_paused("new-token:program")
    assert restarted.has_unknown("new-token:program")


def test_expired_effect_claim_surfaces_unknown_to_stream_worker(tmp_path):
    store = _store(tmp_path / "events.sqlite3", lease_seconds=0.001)
    stream = "copytrade:wallet-a"
    store.enqueue(stream, "signature-a", {}, now=100)
    event = store.claim_next(stream, now=100)
    assert event is not None
    _mark_effect_started(store, event, now=100)

    async def unused_handler(_event):
        raise AssertionError("expired ambiguous effect must not be replayed")

    assert asyncio.run(process_next_event(store, stream, unused_handler)) == "unknown"


def test_schema_v2_is_migrated_without_losing_pending_event(tmp_path):
    import sqlite3

    path = tmp_path / "events-v2.sqlite3"
    store = _store(path)
    store.enqueue("copytrade:wallet-a", "signature-a", {})
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=2")
        db.execute("ALTER TABLE event_inbox DROP COLUMN effect_started")

    migrated = _store(path)
    event = migrated.claim_next("copytrade:wallet-a")
    assert event is not None and event.signature == "signature-a"
    _mark_effect_started(migrated, event)
    assert migrated.metrics()["pending"] == 1


def test_transient_429_retry_keeps_checkpoint_before_ack(tmp_path):
    store = _store(
        tmp_path / "events.sqlite3", retry_base_seconds=3, retry_max_seconds=30
    )
    _enqueue_ordered(store, "new-token:program", "signature-a", now=10)
    event = store.claim_next("new-token:program", now=10)
    assert event is not None

    delay = store.mark_retry(
        event.event_id,
        "enhanced_http_429",
        claim_generation=event.claim_generation,
        now=10,
    )
    assert delay == 3
    assert store.checkpoint("new-token:program") == (0, None)
    assert store.claim_next("new-token:program", now=12) is None
    retried = store.claim_next("new-token:program", now=13)
    assert retried is not None
    _mark_processed(store, retried, now=14)
    assert store.checkpoint("new-token:program") == (1, "signature-a")


def test_duplicate_is_durable_across_restart_and_not_reprocessed(tmp_path):
    path = tmp_path / "events.sqlite3"
    store = _store(path)
    original = _enqueue_ordered(
        store, "protection:wallet-a", "signature-a", {"slot": 5}, slot=5
    )
    claimed = store.claim_next("protection:wallet-a")
    assert claimed is not None
    _mark_processed(store, claimed)

    restarted = _store(path)
    duplicate = restarted.enqueue(
        "protection:wallet-a", "signature-a", {"slot": 5}, slot=5,
        transaction_index=0, is_replay=True
    )

    assert not duplicate.inserted
    assert duplicate.status == "processed"
    assert restarted.claim_next("protection:wallet-a") is None
    assert restarted.metrics()["duplicates"] == 1
    assert restarted.metrics()["replayed"] == 2
    assert restarted.metrics()["duplicate_observation_rate"] == 0.5
    assert restarted.metrics()["replay_observation_rate"] == 0.666667


def test_live_event_without_canonical_transaction_index_is_unknown_and_blocks_after_restart(tmp_path):
    path = tmp_path / "events.sqlite3"
    store = _store(path)
    stream = "new-token:program"
    store.enqueue(stream, "live-signature", {"synthetic": True}, slot=123)
    handler_calls = []

    async def handler(_event):
        handler_calls.append(True)

    assert asyncio.run(process_next_event(store, stream, handler)) == "unknown"
    assert handler_calls == []
    assert store.has_unknown(stream)
    assert store.intake_paused(stream)

    restarted = _store(path)
    assert asyncio.run(process_next_event(restarted, stream, handler)) == "unknown"
    assert handler_calls == []
    with pytest.raises(EventIntakePausedError, match="intake is paused"):
        restarted.enqueue(stream, "later-signature", {}, slot=124)


def test_replay_requires_and_persists_provider_canonical_order(tmp_path):
    store = _store(tmp_path / "events.sqlite3")
    stream = "new-token:program"

    with pytest.raises(EventRecoveryError, match="canonical slot and transaction index"):
        store.enqueue(stream, "missing-index", {}, slot=123, is_replay=True)
    with pytest.raises(ValueError, match="only from history replay"):
        store.enqueue(stream, "live-claimed-index", {}, slot=123, transaction_index=7)

    store.enqueue(
        stream, "canonical-replay", {}, slot=123, transaction_index=7, is_replay=True
    )
    restarted = _store(tmp_path / "events.sqlite3")
    claimed = restarted.claim_next(stream)
    assert claimed is not None
    assert (claimed.slot, claimed.transaction_index) == (123, 7)


def test_schema_v3_migrates_additively_without_changing_unknown_rows(tmp_path):
    path = tmp_path / "events-v3.sqlite3"
    with sqlite3.connect(path) as database:
        database.executescript(
            """
            CREATE TABLE event_streams (
                stream TEXT PRIMARY KEY,
                next_sequence INTEGER NOT NULL DEFAULT 0,
                checkpoint_sequence INTEGER NOT NULL DEFAULT 0,
                checkpoint_signature TEXT,
                recovery_required INTEGER NOT NULL DEFAULT 0,
                intake_paused INTEGER NOT NULL DEFAULT 0,
                blocked_reason TEXT
            );
            CREATE TABLE event_inbox (
                event_id INTEGER PRIMARY KEY,
                stream TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                signature TEXT NOT NULL,
                correlation_id TEXT NOT NULL DEFAULT '',
                slot INTEGER,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending','retry','processing','processed','unknown')
                ),
                attempts INTEGER NOT NULL DEFAULT 0,
                effect_started INTEGER NOT NULL DEFAULT 0,
                next_retry_at REAL NOT NULL DEFAULT 0,
                lease_until REAL NOT NULL DEFAULT 0,
                received_at REAL NOT NULL,
                processed_at REAL,
                last_error_code TEXT,
                UNIQUE(stream, sequence),
                UNIQUE(stream, signature),
                FOREIGN KEY(stream) REFERENCES event_streams(stream)
            );
            CREATE INDEX idx_event_inbox_pending
                ON event_inbox(stream, sequence, status, next_retry_at);
            CREATE TABLE event_counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            INSERT INTO event_streams(stream, next_sequence, intake_paused, blocked_reason)
                VALUES('new-token:program', 1, 1, 'canonical_order_missing');
            INSERT INTO event_inbox(
                event_id, stream, sequence, signature, correlation_id, slot,
                payload_json, status, received_at, last_error_code
            ) VALUES(1, 'new-token:program', 1, 'legacy-unknown', 'corr', 123,
                     '{}', 'unknown', 10, 'canonical_order_missing');
            PRAGMA user_version=3;
            """
        )

    migrated = _store(path)
    with sqlite3.connect(path) as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(event_inbox)")}
        version = database.execute("PRAGMA user_version").fetchone()[0]
        row = database.execute(
            "SELECT status, last_error_code, transaction_index FROM event_inbox WHERE event_id=1"
        ).fetchone()

    assert "transaction_index" in columns
    assert version == 7
    assert row == ("unknown", "canonical_order_missing", None)
    assert migrated.has_unknown("new-token:program")


def test_concurrent_claimants_cannot_claim_same_event(tmp_path):
    path = tmp_path / "events.sqlite3"
    first_store = _store(path)
    second_store = _store(path)
    first_store.enqueue("copytrade:wallet-a", "signature-a", {})
    barrier = Barrier(2)

    def claim(store):
        barrier.wait()
        return store.claim_next("copytrade:wallet-a")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, (first_store, second_store)))

    assert sum(result is not None for result in results) == 1


def test_multiprocess_duplicate_enqueue_is_persisted_once(tmp_path):
    path = tmp_path / "events.sqlite3"
    _store(path)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    workers = [
        context.Process(
            target=_enqueue_synthetic_event_in_process,
            args=(path, barrier, results, "synthetic-shared-signature"),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()

    outcomes = sorted(results.get(timeout=30) for _ in workers)
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    assert outcomes == [(False, "pending"), (True, "pending")]
    with sqlite3.connect(path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM event_inbox WHERE signature=?",
            ("synthetic-shared-signature",),
        ).fetchone()[0]
    assert count == 1


def test_queue_full_pauses_intake_and_blocks_worker_until_history_recovery(tmp_path):
    store = _store(tmp_path / "events.sqlite3", max_pending_events=1)
    _enqueue_ordered(store, "copytrade:wallet-a", "signature-a")

    with pytest.raises(EventQueueFullError):
        store.enqueue("copytrade:wallet-a", "signature-b", {})

    assert store.intake_paused("copytrade:wallet-a")
    assert store.recovery_required_streams("copytrade:") == ["copytrade:wallet-a"]
    assert store.metrics()["queue_full"] == 1
    assert store.metrics()["unpersisted_rejection_rate"] == 0.5

    assert store.claim_next("copytrade:wallet-a") is None
    assert store.pending_count() == 1
    assert store.checkpoint("copytrade:wallet-a") == (0, None)
    assert store.recovery_required_streams("copytrade:") == ["copytrade:wallet-a"]


def test_checkpoint_advances_only_after_all_earlier_events_are_confirmed(tmp_path):
    store = _store(tmp_path / "events.sqlite3")
    _enqueue_ordered(store, "protection:wallet-a", "signature-a", slot=10, index=0)
    _enqueue_ordered(store, "protection:wallet-a", "signature-b", slot=10, index=1)

    first = store.claim_next("protection:wallet-a")
    assert first is not None
    assert store.claim_next("protection:wallet-a") is None
    store.mark_retry(
        first.event_id,
        "parse_timeout",
        claim_generation=first.claim_generation,
        delay_seconds=0,
    )
    retry = store.claim_next("protection:wallet-a")
    assert retry is not None
    _mark_processed(store, retry)

    second = store.claim_next("protection:wallet-a")
    assert second is not None
    assert store.checkpoint("protection:wallet-a") == (1, "signature-a")
    _mark_processed(store, second)
    assert store.checkpoint("protection:wallet-a") == (2, "signature-b")


def test_history_replay_restores_missing_notifications_in_oldest_first_order(tmp_path):
    store = _store(tmp_path / "events.sqlite3")
    stream = "new-token:program"
    _enqueue_ordered(store, stream, "checkpoint-signature", slot=10, index=1)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    _mark_processed(store, checkpoint)
    store.mark_recovery_required(stream, "queue_full")

    async def fetch_page(_address, before, limit):
        assert before is None
        assert limit == 1000
        return [
            {"signature": "signature-c", "slot": 13, "transactionIndex": 4},
            {"signature": "signature-b", "slot": 12, "transactionIndex": 2},
            {"signature": "checkpoint-signature", "slot": 10, "transactionIndex": 1},
        ]

    inserted = asyncio.run(
        replay_stream_history(store, stream, "program-address", fetch_page)
    )

    assert inserted == 2
    assert store.metrics()["replayed"] == 3
    recovery_generation, _boundary = store.recovery_state(stream)
    store.clear_intake_pause(stream, recovery_generation=recovery_generation)
    assert not store.intake_paused(stream)
    first = store.claim_next(stream, recovery=True)
    assert first is not None and first.signature == "signature-b"
    _mark_processed(store, first)
    second = store.claim_next(stream, recovery=True)
    assert second is not None and second.signature == "signature-c"


def test_history_replay_rejects_noncanonical_transaction_order(tmp_path):
    store = _store(tmp_path / "events.sqlite3")
    stream = "new-token:program"
    _enqueue_ordered(store, stream, "checkpoint-signature", slot=10, index=0)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    _mark_processed(store, checkpoint)
    store.mark_recovery_required(stream, "connection_gap")

    async def fetch_page(_address, _before, _limit):
        return [
            {"signature": "newer", "slot": 12, "transactionIndex": 1},
            {"signature": "older", "slot": 13, "transactionIndex": 0},
            {"signature": "checkpoint-signature", "slot": 10, "transactionIndex": 0},
        ]

    with pytest.raises(EventRecoveryError, match="ordering is not canonical"):
        asyncio.run(replay_stream_history(store, stream, "program-address", fetch_page))
    assert store.intake_paused(stream)
    assert store.checkpoint(stream) == (1, "checkpoint-signature")


def test_history_replay_checks_order_across_page_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr("helius_event_store._PAGE_LIMIT", 2)
    store = _store(tmp_path / "events.sqlite3")
    stream = "new-token:program"
    _enqueue_ordered(store, stream, "checkpoint-signature", slot=10, index=0)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    _mark_processed(store, checkpoint)
    store.mark_recovery_required(stream, "connection_gap")

    async def fetch_page(_address, before, _limit):
        if before is None:
            return [
                {"signature": "page-one-newer", "slot": 13, "transactionIndex": 2},
                {"signature": "page-one-older", "slot": 13, "transactionIndex": 1},
            ]
        assert before == "page-one-older"
        return [
            {"signature": "page-two-out-of-order", "slot": 13, "transactionIndex": 3},
            {"signature": "checkpoint-signature", "slot": 10, "transactionIndex": 0},
        ]

    with pytest.raises(EventRecoveryError, match="ordering is not canonical"):
        asyncio.run(replay_stream_history(store, stream, "program-address", fetch_page))
    assert store.intake_paused(stream)
    assert store.checkpoint(stream) == (1, "checkpoint-signature")


def test_history_replay_requires_transaction_index_before_unpausing(tmp_path):
    store = _store(tmp_path / "events.sqlite3")
    stream = "copytrade:wallet-a"
    _enqueue_ordered(store, stream, "checkpoint-signature", slot=10, index=0)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    _mark_processed(store, checkpoint)
    store.mark_recovery_required(stream, "connection_gap")

    async def fetch_page(_address, _before, _limit):
        return [
            {"signature": "newer", "slot": 12},
            {"signature": "checkpoint-signature", "slot": 10, "transactionIndex": 0},
        ]

    with pytest.raises(EventRecoveryError, match="missing canonical"):
        asyncio.run(replay_stream_history(store, stream, "wallet-a", fetch_page))
    assert store.intake_paused(stream)
    assert store.checkpoint(stream) == (1, "checkpoint-signature")


def test_recovery_claim_cannot_bypass_pause_before_history_validation(tmp_path):
    store = _store(tmp_path / "events.sqlite3")
    stream = "copytrade:wallet-a"
    _enqueue_ordered(store, stream, "pending", slot=11, index=1)
    store.mark_recovery_required(stream, "connection_gap")

    assert store.claim_next(stream, recovery=True) is None
    assert store.intake_paused(stream)


def test_recovery_epoch_fences_a_newer_gap_during_history_fetch(tmp_path, monkeypatch):
    store = _store(tmp_path / "events.sqlite3")
    stream = "copytrade:wallet-a"
    _enqueue_ordered(store, stream, "checkpoint", slot=10, index=0)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    _mark_processed(store, checkpoint)
    store.mark_recovery_required(stream, "first_gap")
    first_generation, _ = store.recovery_state(stream)

    async def finalized_history(_address, _before, _limit):
        store.mark_recovery_required(stream, "newer_gap")
        return [
            {"signature": "newer", "slot": 12, "transactionIndex": 1},
            {"signature": "checkpoint", "slot": 10, "transactionIndex": 0},
        ]

    async def handler(_event):
        raise AssertionError("stale recovery must not process events")

    monkeypatch.setattr("helius_event_store.fetch_signature_page", finalized_history)
    _mock_zero_slots(monkeypatch)
    assert not asyncio.run(
        recover_required_streams(store, "copytrade:", handler, lambda _stream: "wallet-a")
    )

    current_generation, boundary = store.recovery_state(stream)
    assert current_generation == first_generation + 1
    assert boundary is None
    assert store.intake_paused(stream)
    assert store.pending_count(stream) == 0


def test_recovery_claim_epoch_fences_effect_start_and_ack(tmp_path):
    store = _store(tmp_path / "events.sqlite3")
    stream = "copytrade:wallet-a"
    _enqueue_ordered(store, stream, "checkpoint", slot=10, index=0)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    _mark_processed(store, checkpoint)
    _enqueue_ordered(store, stream, "recovery-event", slot=11, index=0)
    store.mark_recovery_required(stream, "first_gap")
    recovery_generation, _boundary = store.recovery_state(stream)
    assert store.set_recovery_boundary(
        stream, recovery_generation=recovery_generation, slot=11
    )
    store.clear_intake_pause(stream, recovery_generation=recovery_generation)

    event = store.claim_next(stream, recovery=True)
    assert event is not None
    assert event.recovery_generation == recovery_generation
    store.mark_effect_started(
        event.event_id,
        claim_generation=event.claim_generation,
        recovery=True,
        recovery_generation=event.recovery_generation,
    )

    store.mark_recovery_required(stream, "newer_gap")
    with pytest.raises(EventClaimLostError, match="recovery claim is stale"):
        store.mark_effect_started(
            event.event_id,
            claim_generation=event.claim_generation,
            recovery=True,
            recovery_generation=event.recovery_generation,
        )
    with pytest.raises(EventRecoveryError, match="recovery began"):
        store.mark_processed(
            event.event_id,
            claim_generation=event.claim_generation,
            recovery=True,
            recovery_generation=event.recovery_generation,
        )

    assert store.has_unknown(stream)
    assert store.checkpoint(stream) == (1, "checkpoint")
    assert store.intake_paused(stream)


def test_recovery_waits_for_finality_before_history_scan(tmp_path, monkeypatch):
    store = _store(tmp_path / "events.sqlite3")
    stream = "copytrade:wallet-a"
    _enqueue_ordered(store, stream, "checkpoint", slot=10, index=0)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    _mark_processed(store, checkpoint)
    store.mark_recovery_required(stream, "connection_gap")
    finalized_reads = []
    history_reads = []

    async def finalized_history(_address, _before, _limit):
        history_reads.append(True)
        assert finalized_reads[-1] >= 15
        assert store.recovery_state(stream)[1] == 15
        return [
            {"signature": "newer", "slot": 15, "transactionIndex": 0},
            {"signature": "checkpoint", "slot": 10, "transactionIndex": 0},
        ]

    async def processed_boundary():
        return 15

    async def finalized_slot():
        slot = 14 if not finalized_reads else 15
        finalized_reads.append(slot)
        return slot

    callbacks = []

    async def handler(event):
        callbacks.append(event.signature)

    monkeypatch.setattr("helius_event_store.fetch_signature_page", finalized_history)
    monkeypatch.setattr("helius_event_store.fetch_processed_slot", processed_boundary)
    monkeypatch.setattr("helius_event_store.fetch_finalized_slot", finalized_slot)
    monkeypatch.setattr("helius_event_store._FINALITY_POLL_SECONDS", 0)
    assert asyncio.run(
        recover_required_streams(store, "copytrade:", handler, lambda _stream: "wallet-a")
    )
    assert history_reads
    assert store.recovery_state(stream) is None
    assert not store.intake_paused(stream)
    assert callbacks == ["newer"]


def test_operator_stop_during_finality_wait_keeps_stream_paused(tmp_path, monkeypatch):
    store = _store(tmp_path / "events.sqlite3")
    stream = "copytrade:wallet-a"
    _enqueue_ordered(store, stream, "checkpoint", slot=10, index=0)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    _mark_processed(store, checkpoint)
    store.mark_recovery_required(stream, "connection_gap")
    running = {"value": True}
    history_reads = []

    async def processed_boundary():
        return 15

    async def finalized_slot():
        running["value"] = False
        return 14

    async def finalized_history(_address, _before, _limit):
        history_reads.append(True)
        return [{"signature": "checkpoint", "slot": 10, "transactionIndex": 0}]

    async def handler(_event):
        raise AssertionError("stop during recovery must not start an effect")

    monkeypatch.setattr("helius_event_store.fetch_processed_slot", processed_boundary)
    monkeypatch.setattr("helius_event_store.fetch_finalized_slot", finalized_slot)
    monkeypatch.setattr("helius_event_store.fetch_signature_page", finalized_history)
    monkeypatch.setattr("helius_event_store._FINALITY_POLL_SECONDS", 0)

    assert not asyncio.run(
        recover_required_streams(
            store,
            "copytrade:",
            handler,
            lambda _stream: "wallet-a",
            should_stop=lambda: not running["value"],
        )
    )
    assert history_reads == []
    assert store.recovery_state(stream) is not None
    assert store.intake_paused(stream)


@pytest.mark.parametrize("blocked_call", ["processed", "finalized"])
def test_operator_stop_cancels_inflight_slot_read(tmp_path, monkeypatch, blocked_call):
    store = _store(tmp_path / f"events-{blocked_call}.sqlite3")
    stream = "copytrade:wallet-a"
    _enqueue_ordered(store, stream, "checkpoint", slot=10, index=0)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    _mark_processed(store, checkpoint)
    store.mark_recovery_required(stream, "connection_gap")

    running = {"value": True}
    started = asyncio.Event()
    cancelled = asyncio.Event()
    history_reads = []

    async def processed_boundary():
        if blocked_call == "processed":
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return 15

    async def finalized_slot():
        if blocked_call == "finalized":
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return 15

    async def history_page(_address, _before, _limit):
        history_reads.append(True)
        return [{"signature": "checkpoint", "slot": 10, "transactionIndex": 0}]

    monkeypatch.setattr("helius_event_store.fetch_processed_slot", processed_boundary)
    monkeypatch.setattr("helius_event_store.fetch_finalized_slot", finalized_slot)
    monkeypatch.setattr("helius_event_store.fetch_signature_page", history_page)

    async def scenario():
        recovery = asyncio.create_task(
            recover_required_streams(
                store,
                "copytrade:",
                lambda _event: asyncio.sleep(0),
                lambda _stream: "wallet-a",
                should_stop=lambda: not running["value"],
            )
        )
        await started.wait()
        running["value"] = False
        assert await recovery is False

    asyncio.run(scenario())
    assert cancelled.is_set()
    assert history_reads == []
    assert store.recovery_state(stream) is not None
    assert store.intake_paused(stream)
    assert store.checkpoint(stream) == (1, "checkpoint")


def test_drain_stops_before_claiming_another_event(tmp_path):
    store = _store(tmp_path / "events.sqlite3")
    stream = "copytrade:wallet-a"
    _enqueue_ordered(store, stream, "checkpoint", slot=10, index=0)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    _mark_processed(store, checkpoint)
    _enqueue_ordered(store, stream, "first", slot=11, index=0)
    _enqueue_ordered(store, stream, "second", slot=12, index=0)
    running = {"value": True}
    handled = []

    async def handler(event):
        handled.append(event.signature)
        running["value"] = False

    result = asyncio.run(
        drain_stream(
            store, stream, handler, should_stop=lambda: not running["value"]
        )
    )

    assert result == "stopped"
    assert handled == ["first"]
    assert store.pending_count(stream) == 1


def test_repeated_finality_rpc_failures_return_paused_for_reconnect(tmp_path, monkeypatch):
    store = _store(tmp_path / "events.sqlite3")
    stream = "copytrade:wallet-a"
    _enqueue_ordered(store, stream, "checkpoint", slot=10, index=0)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    _mark_processed(store, checkpoint)
    store.mark_recovery_required(stream, "connection_gap")
    failure_count = []

    async def processed_boundary():
        return 15

    async def finalized_slot():
        failure_count.append(True)
        raise TimeoutError("private RPC endpoint detail")

    async def handler(_event):
        raise AssertionError("RPC failure must not drain events")

    monkeypatch.setattr("helius_event_store.fetch_processed_slot", processed_boundary)
    monkeypatch.setattr("helius_event_store.fetch_finalized_slot", finalized_slot)
    monkeypatch.setattr("helius_event_store._FINALITY_POLL_SECONDS", 0)

    assert not asyncio.run(
        recover_required_streams(
            store, "copytrade:", handler, lambda _stream: "wallet-a"
        )
    )
    assert len(failure_count) == 3
    assert store.recovery_state(stream) is not None
    assert store.intake_paused(stream)


def test_inactive_address_can_recover_after_global_finality_catches_up(
    tmp_path, monkeypatch
):
    store = _store(tmp_path / "events.sqlite3")
    stream = "copytrade:inactive-wallet"
    _enqueue_ordered(store, stream, "checkpoint", slot=10, index=0)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    _mark_processed(store, checkpoint)
    store.mark_recovery_required(stream, "connection_gap")

    async def processed_boundary():
        return 15

    async def finalized_slot():
        return 15

    async def finalized_history(_address, _before, _limit):
        return [{"signature": "checkpoint", "slot": 10, "transactionIndex": 0}]

    async def handler(_event):
        raise AssertionError("an inactive address has no new event to process")

    monkeypatch.setattr("helius_event_store.fetch_processed_slot", processed_boundary)
    monkeypatch.setattr("helius_event_store.fetch_finalized_slot", finalized_slot)
    monkeypatch.setattr("helius_event_store.fetch_signature_page", finalized_history)
    assert asyncio.run(
        recover_required_streams(
            store, "copytrade:", handler, lambda _stream: "inactive-wallet"
        )
    )
    assert store.recovery_state(stream) is None
    assert not store.intake_paused(stream)


def test_subscription_ack_wait_buffers_interleaved_notifications():
    import json
    from helius_event_store import await_subscription_acknowledgements

    notification = json.dumps({"jsonrpc": "2.0", "method": "logsNotification"})

    class FakeWebSocket:
        def __init__(self):
            self.frames = iter(
                [
                    notification,
                    json.dumps({"jsonrpc": "2.0", "id": 7, "result": 99}),
                ]
            )

        async def recv(self):
            return next(self.frames)

    pending = {7: "copytrade:wallet-a"}
    handled_acks = []

    async def acknowledge(message):
        handled_acks.append(message)
        pending.pop(json.loads(message)["id"])

    buffered = asyncio.run(
        await_subscription_acknowledgements(FakeWebSocket(), pending, acknowledge)
    )
    assert buffered == [notification]
    assert pending == {}
    assert len(handled_acks) == 1


@pytest.mark.parametrize(("invalid_id", "pending_id"), [(True, 1), (7.0, 7)])
def test_subscription_ack_rejects_non_integer_ids_that_equal_integer(
    invalid_id, pending_id
):
    import json
    from helius_event_store import await_subscription_acknowledgements

    class FakeWebSocket:
        async def recv(self):
            return json.dumps({"jsonrpc": "2.0", "id": invalid_id, "result": 99})

    pending = {pending_id: "copytrade:wallet-a"}

    async def acknowledge(_message):
        raise AssertionError("non-integer acknowledgement must be refused")

    with pytest.raises(EventRecoveryError, match="acknowledgement ID is invalid"):
        asyncio.run(await_subscription_acknowledgements(FakeWebSocket(), pending, acknowledge))


@pytest.mark.parametrize(
    "acknowledgement",
    [
        {"jsonrpc": "2.0", "id": 7},
        {"jsonrpc": "2.0", "id": 7, "error": {"code": -1}},
        {"jsonrpc": "2.0", "id": 7, "result": "99"},
        {"jsonrpc": "2.0", "id": 7, "result": True},
    ],
)
def test_subscription_ack_rejects_missing_or_malformed_result(acknowledgement):
    import json
    from helius_event_store import await_subscription_acknowledgements

    class FakeWebSocket:
        async def recv(self):
            return json.dumps(acknowledgement)

    pending = {7: "copytrade:wallet-a"}
    acknowledged = []

    async def acknowledge(message):
        acknowledged.append(message)
        pending.pop(json.loads(message)["id"])

    with pytest.raises(EventRecoveryError, match="acknowledgement was rejected"):
        asyncio.run(
            await_subscription_acknowledgements(
                FakeWebSocket(), pending, acknowledge
            )
        )
    assert pending == {7: "copytrade:wallet-a"}
    assert acknowledged == []


def test_history_replay_fails_closed_if_checkpoint_cannot_be_found(tmp_path):
    store = _store(tmp_path / "events.sqlite3")
    stream = "protection:wallet-a"
    _enqueue_ordered(store, stream, "checkpoint-signature", slot=10, index=0)
    event = store.claim_next(stream)
    assert event is not None
    _mark_processed(store, event)
    store.mark_recovery_required(stream)

    async def missing_checkpoint(_address, _before, _limit):
        return [{"signature": "unrelated-signature", "slot": 20, "transactionIndex": 1}]

    with pytest.raises(EventRecoveryError, match="checkpoint"):
        asyncio.run(
            replay_stream_history(store, stream, "wallet-a", missing_checkpoint)
        )
    assert store.intake_paused(stream)
    assert store.recovery_required_streams("protection:") == [stream]


def test_missing_order_unknown_is_reconciled_and_drained_in_canonical_order(
    tmp_path, monkeypatch
):
    store = _store(tmp_path / "events.sqlite3")
    stream = "copytrade:wallet-a"
    _enqueue_ordered(store, stream, "checkpoint-signature", slot=100, index=1)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    _mark_processed(store, checkpoint)

    store.enqueue(stream, "live-earlier", {"live": True}, slot=101)
    callbacks = []

    async def handler(event):
        store.mark_effect_started(
            event.event_id,
            claim_generation=event.claim_generation,
            recovery=event.recovery_claim,
            recovery_generation=event.recovery_generation,
        )
        callbacks.append(event.signature)

    assert asyncio.run(process_next_event(store, stream, handler)) == "unknown"
    assert callbacks == []
    store.mark_recovery_required(stream, "connection_gap")

    async def finalized_history(_address, _before, _limit):
        return [
            {"signature": "live-later", "slot": 101, "transactionIndex": 3},
            {"signature": "live-earlier", "slot": 101, "transactionIndex": 2},
            {"signature": "checkpoint-signature", "slot": 100, "transactionIndex": 1},
        ]

    monkeypatch.setattr("helius_event_store.fetch_signature_page", finalized_history)
    _mock_zero_slots(monkeypatch)
    assert asyncio.run(
        recover_required_streams(store, "copytrade:", handler, lambda _stream: "wallet-a")
    )
    assert callbacks == ["live-earlier", "live-later"]
    assert store.checkpoint(stream) == (3, "live-later")
    assert not store.intake_paused(stream)
    assert store.recovery_required_streams("copytrade:") == []


def test_unknown_with_possible_effect_is_never_promoted_from_history(tmp_path, monkeypatch):
    store = _store(tmp_path / "events.sqlite3")
    stream = "protection:wallet-a"
    _enqueue_ordered(store, stream, "checkpoint-signature", slot=100, index=1)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    _mark_processed(store, checkpoint)

    store.enqueue(stream, "ambiguous-effect", {"live": True}, slot=101)
    event = store.claim_next(stream)
    assert event is not None
    _mark_effect_started(store, event)
    _mark_unknown(store, event, "handler_outcome_unknown")
    store.mark_recovery_required(stream, "connection_gap")
    fetch_calls = []

    async def forbidden_history(*_args):
        fetch_calls.append(True)
        return []

    monkeypatch.setattr("helius_event_store.fetch_signature_page", forbidden_history)
    _mock_zero_slots(monkeypatch)

    async def handler(_event):
        raise AssertionError("ambiguous UNKNOWN must not be replayed")

    assert not asyncio.run(
        recover_required_streams(store, "protection:", handler, lambda _stream: "wallet-a")
    )
    assert fetch_calls == []
    assert store.has_unknown(stream)
    assert store.intake_paused(stream)


def test_recovery_replays_interleaved_gap_before_processing_queued_later_event(
    tmp_path, monkeypatch
):
    store = _store(tmp_path / "events.sqlite3")
    stream = "copytrade:wallet-a"
    _enqueue_ordered(store, stream, "checkpoint", slot=100, index=1)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    _mark_processed(store, checkpoint)
    _enqueue_ordered(store, stream, "already-queued-later", slot=101, index=3)
    store.mark_recovery_required(stream, "connection_gap")

    callbacks = []

    async def handler(event):
        callbacks.append(event.signature)

    assert store.claim_next(stream) is None
    assert asyncio.run(drain_stream(store, stream, handler)) == "idle"
    assert callbacks == []

    async def finalized_history(_address, _before, _limit):
        return [
            {"signature": "already-queued-later", "slot": 101, "transactionIndex": 3},
            {"signature": "missed-earlier", "slot": 101, "transactionIndex": 2},
            {"signature": "checkpoint", "slot": 100, "transactionIndex": 1},
        ]

    monkeypatch.setattr("helius_event_store.fetch_signature_page", finalized_history)
    _mock_zero_slots(monkeypatch)
    assert asyncio.run(
        recover_required_streams(store, "copytrade:", handler, lambda _stream: "wallet-a")
    )
    assert callbacks == ["missed-earlier", "already-queued-later"]
    assert store.checkpoint(stream) == (3, "already-queued-later")
    assert store.recovery_required_streams("copytrade:") == []


def test_replay_refuses_checkpoint_tuple_conflict(tmp_path):
    store = _store(tmp_path / "events.sqlite3")
    stream = "protection:wallet-a"
    _enqueue_ordered(store, stream, "checkpoint", slot=100, index=1)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    _mark_processed(store, checkpoint)
    store.mark_recovery_required(stream, "connection_gap")

    async def conflicting_history(_address, _before, _limit):
        return [{"signature": "checkpoint", "slot": 100, "transactionIndex": 2}]

    with pytest.raises(EventRecoveryError, match="conflicts with the durable checkpoint"):
        asyncio.run(
            replay_stream_history(store, stream, "wallet-a", conflicting_history)
        )
    assert store.checkpoint(stream) == (1, "checkpoint")
    assert store.intake_paused(stream)


def test_expired_worker_cannot_start_effect_or_ack_after_reclaim(tmp_path):
    store = _store(tmp_path / "events.sqlite3", lease_seconds=2)
    stream = "protection:wallet-a"
    _enqueue_ordered(store, stream, "shared-event", slot=100, index=1)
    stale = store.claim_next(stream, now=100)
    assert stale is not None
    assert store.claim_next(stream, now=101) is None
    current = store.claim_next(stream, now=103)
    assert current is not None
    assert current.claim_generation == stale.claim_generation + 1

    effect_generations = []
    with pytest.raises(EventClaimLostError, match="claim is no longer active"):
        store.mark_effect_started(
            stale.event_id, claim_generation=stale.claim_generation, now=103
        )
    store.mark_effect_started(
        current.event_id, claim_generation=current.claim_generation, now=103
    )
    effect_generations.append(current.claim_generation)
    with pytest.raises(EventClaimLostError, match="claim is no longer active"):
        store.mark_processed(
            stale.event_id,
            claim_generation=stale.claim_generation,
            now=104,
        )
    assert effect_generations == [current.claim_generation]
    assert store.metrics()["unknown"] == 0

    store.mark_processed(
        current.event_id,
        claim_generation=current.claim_generation,
        now=104,
    )
    assert store.checkpoint(stream) == (1, "shared-event")


def test_schema_v4_migrates_claim_generation_additively(tmp_path):
    path = tmp_path / "events-v4.sqlite3"
    store = _store(path)
    _enqueue_ordered(store, "migration-stream", "pending", slot=1, index=0)
    with sqlite3.connect(path) as database:
        database.execute("ALTER TABLE event_inbox DROP COLUMN claim_generation")
        database.execute("PRAGMA user_version=4")

    migrated = _store(path)
    with sqlite3.connect(path) as database:
        version = database.execute("PRAGMA user_version").fetchone()[0]
        columns = {
            str(row[1]) for row in database.execute("PRAGMA table_info(event_inbox)")
        }
    assert version == 7
    assert "claim_generation" in columns
    with sqlite3.connect(path) as database:
        stream_columns = {
            str(row[1]) for row in database.execute("PRAGMA table_info(event_streams)")
        }
    assert {"recovery_generation", "recovery_boundary_slot"} <= stream_columns
    claimed = migrated.claim_next("migration-stream")
    assert claimed is not None and claimed.claim_generation == 1


def test_legacy_processed_prefix_without_order_blocks_checkpoint_advance(tmp_path):
    path = tmp_path / "events.sqlite3"
    store = _store(path)
    stream = "copytrade:wallet-a"
    store.enqueue(stream, "legacy-processed", {}, slot=100)
    with sqlite3.connect(path) as database:
        database.execute(
            "UPDATE event_inbox SET status='processed', payload_json='', processed_at=1 "
            "WHERE stream=? AND signature=?",
            (stream, "legacy-processed"),
        )
        database.execute(
            "UPDATE event_streams SET checkpoint_sequence=1, checkpoint_signature=? "
            "WHERE stream=?",
            ("legacy-processed", stream),
        )
    _enqueue_ordered(store, stream, "new-canonical", slot=101, index=2)
    event = store.claim_next(stream)
    assert event is not None and event.signature == "new-canonical"

    with pytest.raises(EventRecoveryError, match="Canonical checkpoint"):
        _mark_processed(store, event)

    assert store.checkpoint(stream) == (1, "legacy-processed")
    assert store.has_unknown(stream)
    assert store.intake_paused(stream)
    assert store.recovery_required_streams("copytrade:") == [stream]
    with pytest.raises(EventRecoveryError, match="UNKNOWN event blocks"):
        store.clear_intake_pause(
            stream, recovery_generation=store.recovery_state(stream)[0]
        )


def test_multiprocess_history_reconciliation_is_idempotent_and_processes_once(tmp_path):
    path = tmp_path / "events.sqlite3"
    store = _store(path)
    _enqueue_ordered(store, "fixture-stream", "checkpoint", slot=122, index=6)
    baseline = store.claim_next("fixture-stream")
    assert baseline is not None
    _mark_processed(store, baseline)
    store.enqueue("fixture-stream", "shared-signature", {}, slot=123)
    event = store.claim_next("fixture-stream")
    assert event is not None
    _mark_unknown(store, event, "canonical_order_missing")

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    workers = [
        context.Process(
            target=_reconcile_unknown_in_process,
            args=(path, barrier, results, "shared-signature"),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    outcomes = sorted(results.get(timeout=30) for _ in workers)
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    assert outcomes == [(False, "retry"), (False, "retry")]
    with sqlite3.connect(path) as database:
        rows = database.execute(
            "SELECT COUNT(*), MIN(status), MIN(slot), MIN(transaction_index) "
            "FROM event_inbox WHERE stream='fixture-stream' AND signature='shared-signature'"
        ).fetchone()
    assert rows == (1, "retry", 123, 7)

    async def finalized_history(_address, _before, _limit):
        return [
            {"signature": "shared-signature", "slot": 123, "transactionIndex": 7},
            {"signature": "checkpoint", "slot": 122, "transactionIndex": 6},
        ]

    assert asyncio.run(
        replay_stream_history(
            _store(path), "fixture-stream", "wallet-a", finalized_history
        )
    ) == 0
    replayed_store = _store(path)
    recovery_generation, _boundary = replayed_store.recovery_state("fixture-stream")
    replayed_store.clear_intake_pause(
        "fixture-stream", recovery_generation=recovery_generation
    )

    callbacks = []

    async def handler(record):
        callbacks.append(record.signature)

    assert asyncio.run(
        process_next_event(_store(path), "fixture-stream", handler, recovery=True)
    ) == "processed"
    restarted = _store(path)
    recovery_generation, _boundary = restarted.recovery_state("fixture-stream")
    assert restarted.complete_recovery_if_idle(
        "fixture-stream", recovery_generation=recovery_generation
    )
    assert asyncio.run(process_next_event(restarted, "fixture-stream", handler)) == "idle"
    assert callbacks == ["shared-signature"]


def test_signature_page_rpc_uses_finalized_read_only_history(monkeypatch):
    import rpc_client
    from helius_event_store import fetch_signature_page

    calls = []

    async def fake_rpc(payload, *, timeout):
        calls.append((payload, timeout))
        return [{"signature": "sig", "slot": 1, "transactionIndex": 0}]

    monkeypatch.setattr(rpc_client, "rpc_post_strict", fake_rpc)
    result = asyncio.run(fetch_signature_page("wallet-a", "before-signature", 1500))

    assert result == [{"signature": "sig", "slot": 1, "transactionIndex": 0}]
    payload, timeout = calls[0]
    assert payload["method"] == "getSignaturesForAddress"
    assert payload["params"] == [
        "wallet-a",
        {"limit": 1000, "commitment": "finalized", "before": "before-signature"},
    ]
    assert timeout == 15


def test_finalized_slot_rpc_is_read_only_and_strict(monkeypatch):
    import rpc_client
    from helius_event_store import fetch_finalized_slot

    calls = []

    async def fake_rpc(payload, *, timeout):
        calls.append((payload, timeout))
        return 456

    monkeypatch.setattr(rpc_client, "rpc_post_strict", fake_rpc)
    assert asyncio.run(fetch_finalized_slot()) == 456
    payload, timeout = calls[0]
    assert payload["method"] == "getSlot"
    assert payload["params"] == [{"commitment": "finalized"}]
    assert timeout == 12


def test_finalized_slot_rpc_rejects_boolean_and_negative_result(monkeypatch):
    import rpc_client
    from helius_event_store import fetch_finalized_slot

    async def fake_rpc(_payload, *, timeout):
        return True

    monkeypatch.setattr(rpc_client, "rpc_post_strict", fake_rpc)
    with pytest.raises(EventRecoveryError, match="invalid boundary"):
        asyncio.run(fetch_finalized_slot())


def test_existing_stream_without_checkpoint_fails_closed(tmp_path):
    store = _store(tmp_path / "events.sqlite3")
    stream = "new-token:program"
    store.ensure_stream(stream)
    store.mark_recovery_required(stream, "connection_gap")

    async def handler(_event):
        raise AssertionError("no event exists to process")

    assert not asyncio.run(
        recover_required_streams(
            store, "new-token:", handler, lambda name: name.removeprefix("new-token:")
        )
    )
    assert store.intake_paused(stream)
    assert store.recovery_required_streams("new-token:") == [stream]
    assert store.blocked_reason(stream) == "fresh_stream_bootstrap_required"


def test_fresh_stream_requires_manual_bootstrap_before_live_intake(tmp_path):
    store = _store(tmp_path / "events.sqlite3")
    stream = "new-token:program"
    store.ensure_stream(stream)
    assert store.intake_paused(stream)
    assert store.blocked_reason(stream) == "fresh_stream_bootstrap_required"
    with pytest.raises(EventIntakePausedError):
        store.enqueue(stream, "first-live-signature", {})

    async def handler(_event):
        raise AssertionError("fresh stream has no trusted checkpoint")

    assert not asyncio.run(
        recover_required_streams(
            store, "new-token:", handler, lambda name: name.removeprefix("new-token:")
        )
    )
    assert store.recovery_required_streams("new-token:") == [stream]
    assert store.blocked_reason(stream) == "fresh_stream_bootstrap_required"
    assert store.metrics()["paused_intake_rejections"] == 1


def test_ensure_stream_gates_legacy_uncheckpointed_stream(tmp_path):
    store = _store(tmp_path / "events.sqlite3")
    stream = "copytrade:legacy-wallet"
    store.enqueue(stream, "legacy-pending-event", {})
    assert not store.intake_paused(stream)

    store.ensure_stream(stream)

    assert store.intake_paused(stream)
    assert store.recovery_required_streams("copytrade:") == [stream]
    assert store.blocked_reason(stream) == "fresh_stream_bootstrap_required"


def test_stop_during_history_pagination_avoids_extra_pages_and_effects(
    tmp_path, monkeypatch
):
    store = _store(tmp_path / "events.sqlite3")
    stream = "copytrade:wallet-a"
    _enqueue_ordered(store, stream, "checkpoint", slot=10, index=0)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    _mark_processed(store, checkpoint)
    store.mark_recovery_required(stream, "connection_gap")

    running = {"value": True}
    page_calls = []
    page = [
        {"signature": f"page-{i}", "slot": 1000 - i, "transactionIndex": 0}
        for i in range(1000)
    ]

    async def fetch_page(_address, _before, limit):
        page_calls.append(limit)
        running["value"] = False
        return page

    monkeypatch.setattr("helius_event_store.fetch_signature_page", fetch_page)
    monkeypatch.setattr("helius_event_store.fetch_processed_slot", _slot_zero)
    monkeypatch.setattr("helius_event_store.fetch_finalized_slot", _slot_zero)

    async def handler(_event):
        raise AssertionError("shutdown must not start an event effect")

    assert not asyncio.run(
        recover_required_streams(
            store,
            "copytrade:",
            handler,
            lambda _stream: "wallet-a",
            should_stop=lambda: not running["value"],
        )
    )
    assert page_calls == [1000]
    assert store.intake_paused(stream)
    assert store.recovery_required_streams("copytrade:") == [stream]
    assert store.checkpoint(stream) == (1, "checkpoint")
    assert store.pending_count(stream) == 0


def test_stop_cancels_inflight_readonly_history_page(tmp_path):
    store = _store(tmp_path / "events.sqlite3")
    stream = "copytrade:wallet-a"
    _enqueue_ordered(store, stream, "checkpoint", slot=10, index=0)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    _mark_processed(store, checkpoint)
    store.mark_recovery_required(stream, "connection_gap")

    running = {"value": True}
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_page(_address, _before, _limit):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def scenario():
        recovery = asyncio.create_task(
            replay_stream_history(
                store,
                stream,
                "wallet-a",
                slow_page,
                should_stop=lambda: not running["value"],
            )
        )
        await started.wait()
        running["value"] = False
        with pytest.raises(EventRecoveryError, match="stopped before completion"):
            await recovery

    asyncio.run(scenario())
    assert cancelled.is_set()
    assert store.intake_paused(stream)
    assert store.checkpoint(stream) == (1, "checkpoint")


def test_mocked_429_during_history_replay_keeps_stream_paused(tmp_path):
    store = _store(tmp_path / "events.sqlite3")
    stream = "copytrade:wallet-a"
    _enqueue_ordered(store, stream, "checkpoint", slot=10, index=0)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    _mark_processed(store, checkpoint)
    store.mark_recovery_required(stream, "connection_gap")
    calls = []

    async def throttled(_address, _before, _limit):
        calls.append(True)
        raise RuntimeError("HTTP 429 provider detail must not escape")

    with pytest.raises(EventRecoveryError, match="temporarily unavailable") as exc_info:
        asyncio.run(replay_stream_history(store, stream, "wallet-a", throttled))
    assert "429" not in str(exc_info.value)
    assert calls == [True]
    assert store.intake_paused(stream)
    assert store.checkpoint(stream) == (1, "checkpoint")


def test_queue_saturation_fails_closed_when_gap_exceeds_inbox_bound(tmp_path, monkeypatch):
    store = _store(tmp_path / "events.sqlite3", max_pending_events=2)
    stream = "copytrade:wallet-a"
    _enqueue_ordered(store, stream, "checkpoint-signature", slot=10, index=1)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    _mark_processed(store, checkpoint)
    _enqueue_ordered(store, stream, "pending-a", slot=11, index=2)
    _enqueue_ordered(store, stream, "pending-b", slot=12, index=3)
    with pytest.raises(EventQueueFullError):
        store.enqueue(stream, "missed-at-capacity", {})

    async def fetch_page(_address, _before, _limit):
        return [
            {"signature": "missed-at-capacity", "slot": 13, "transactionIndex": 4},
            {"signature": "pending-b", "slot": 12, "transactionIndex": 3},
            {"signature": "pending-a", "slot": 11, "transactionIndex": 2},
            {"signature": "checkpoint-signature", "slot": 10, "transactionIndex": 1},
        ]

    monkeypatch.setattr("helius_event_store.fetch_signature_page", fetch_page)
    _mock_zero_slots(monkeypatch)
    callbacks = []

    async def handler(event):
        callbacks.append(event.signature)

    assert not asyncio.run(
        recover_required_streams(store, "copytrade:", handler, lambda _stream: "wallet-a")
    )
    assert callbacks == []
    assert store.pending_count(stream) == 2
    assert store.checkpoint(stream) == (1, "checkpoint-signature")
    assert store.intake_paused(stream)
    assert store.recovery_required_streams("copytrade:") == [stream]


def test_processing_records_one_correlated_end_to_end_metric(tmp_path, monkeypatch):
    path = tmp_path / "metrics.jsonl"
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(path))
    store = _store(tmp_path / "events.sqlite3")
    store.enqueue(
        "new-token:program", "signature-private-marker", {}, slot=123,
        transaction_index=1, is_replay=True, now=time.time()
    )

    async def handler(_event):
        return None

    assert asyncio.run(
        process_next_event(store, "new-token:program", handler)
    ) == "processed"

    summarize_metrics(path)
    rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    enqueue = [row for row in rows if '"stage":"event.inbox_enqueue"' in row]
    processing = [row for row in rows if '"stage":"event.processing"' in row]
    end_to_end = [row for row in rows if '"stage":"event.end_to_end"' in row]
    assert enqueue and processing and end_to_end
    assert '"correlation_id":"' in processing[0]
    correlation = processing[0].split('"correlation_id":"')[1].split('"')[0]
    assert correlation == enqueue[0].split('"correlation_id":"')[1].split('"')[0]
    assert correlation == end_to_end[0].split('"correlation_id":"')[1].split('"')[0]
    assert "signature-private-marker" not in path.read_text(encoding="utf-8")


def test_retryable_parse_failure_does_not_advance_checkpoint(tmp_path):
    store = _store(tmp_path / "events.sqlite3", retry_base_seconds=1)
    store.enqueue(
        "copytrade:wallet-a", "signature-a", {}, slot=123,
        transaction_index=1, is_replay=True,
    )

    async def unavailable(_event):
        raise RetryableEventError("enhanced_http_429")

    assert asyncio.run(
        process_next_event(store, "copytrade:wallet-a", unavailable)
    ) == "retry"
    assert store.checkpoint("copytrade:wallet-a") == (0, None)
    assert store.metrics()["retried"] == 1


def test_handler_exception_blocks_stream_as_unknown(tmp_path):
    store = _store(tmp_path / "events.sqlite3")
    _enqueue_ordered(store, "copytrade:wallet-a", "signature-a")

    async def uncertain(_event):
        raise RuntimeError("downstream may have partially completed")

    assert asyncio.run(
        process_next_event(store, "copytrade:wallet-a", uncertain)
    ) == "unknown"
    assert store.checkpoint("copytrade:wallet-a") == (0, None)
    assert store.metrics()["unknown"] == 1
    assert store.claim_next("copytrade:wallet-a") is None
