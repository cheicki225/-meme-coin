import asyncio
import multiprocessing
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from helius_event_store import (
    EventQueueFullError,
    EventIntakePausedError,
    EventRecoveryError,
    HeliusEventStore,
    RetryableEventError,
    process_next_event,
    recover_required_streams,
    replay_stream_history,
)
from execution_metrics import summarize_metrics


def _store(path, **kwargs):
    return HeliusEventStore(path, **kwargs)


def _enqueue_synthetic_event_in_process(path, barrier, result_queue, signature):
    store = HeliusEventStore(path)
    barrier.wait(timeout=20)
    result = store.enqueue(
        "fixture-stream", signature, {"synthetic": True}, slot=123
    )
    result_queue.put((result.inserted, result.status))


@pytest.fixture(autouse=True)
def _isolate_metrics_path(monkeypatch, tmp_path):
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(tmp_path / "metrics.jsonl"))


def test_event_is_persisted_before_processing_and_resumes_after_crash(tmp_path):
    path = tmp_path / "helius-events.sqlite3"
    store = _store(path, lease_seconds=2)
    inserted = store.enqueue(
        "copytrade:wallet-a", "signature-a", {"is_swap_related": True}, slot=10, now=100
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
    restarted.mark_processed(recovered.event_id, now=104)
    assert restarted.checkpoint("copytrade:wallet-a") == (1, "signature-a")


def test_expired_claim_after_effect_boundary_becomes_unknown_on_restart(tmp_path):
    path = tmp_path / "events.sqlite3"
    store = _store(path, lease_seconds=2)
    store.enqueue("new-token:program", "signature-a", {}, now=100)
    claimed = store.claim_next("new-token:program", now=100)
    assert claimed is not None
    store.mark_effect_started(claimed.event_id)

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
    store.mark_effect_started(event.event_id)

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
    migrated.mark_effect_started(event.event_id)
    assert migrated.metrics()["pending"] == 1


def test_transient_429_retry_keeps_checkpoint_before_ack(tmp_path):
    store = _store(
        tmp_path / "events.sqlite3", retry_base_seconds=3, retry_max_seconds=30
    )
    store.enqueue("new-token:program", "signature-a", {}, now=10)
    event = store.claim_next("new-token:program", now=10)
    assert event is not None

    delay = store.mark_retry(event.event_id, "enhanced_http_429", now=10)
    assert delay == 3
    assert store.checkpoint("new-token:program") == (0, None)
    assert store.claim_next("new-token:program", now=12) is None
    retried = store.claim_next("new-token:program", now=13)
    assert retried is not None
    store.mark_processed(retried.event_id, now=14)
    assert store.checkpoint("new-token:program") == (1, "signature-a")


def test_duplicate_is_durable_across_restart_and_not_reprocessed(tmp_path):
    path = tmp_path / "events.sqlite3"
    store = _store(path)
    original = store.enqueue("protection:wallet-a", "signature-a", {"slot": 5})
    claimed = store.claim_next("protection:wallet-a")
    assert claimed is not None
    store.mark_processed(claimed.event_id)

    restarted = _store(path)
    duplicate = restarted.enqueue(
        "protection:wallet-a", "signature-a", {"slot": 5}, slot=5,
        transaction_index=0, is_replay=True
    )

    assert not duplicate.inserted
    assert duplicate.status == "processed"
    assert restarted.claim_next("protection:wallet-a") is None
    assert restarted.metrics()["duplicates"] == 1
    assert restarted.metrics()["replayed"] == 1
    assert restarted.metrics()["duplicate_observation_rate"] == 0.5
    assert restarted.metrics()["replay_observation_rate"] == 0.5


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
    assert version == 4
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


def test_queue_full_pauses_intake_and_marks_replay_required(tmp_path):
    store = _store(tmp_path / "events.sqlite3", max_pending_events=1)
    store.enqueue("copytrade:wallet-a", "signature-a", {})

    with pytest.raises(EventQueueFullError):
        store.enqueue("copytrade:wallet-a", "signature-b", {})

    assert store.intake_paused("copytrade:wallet-a")
    assert store.recovery_required_streams("copytrade:") == ["copytrade:wallet-a"]
    assert store.metrics()["queue_full"] == 1
    assert store.metrics()["unpersisted_rejection_rate"] == 0.5

    first = store.claim_next("copytrade:wallet-a")
    assert first is not None
    store.mark_processed(first.event_id)
    assert store.pending_count() == 0
    assert store.recovery_required_streams("copytrade:") == ["copytrade:wallet-a"]


def test_checkpoint_advances_only_after_all_earlier_events_are_confirmed(tmp_path):
    store = _store(tmp_path / "events.sqlite3")
    store.enqueue("protection:wallet-a", "signature-a", {})
    store.enqueue("protection:wallet-a", "signature-b", {})

    first = store.claim_next("protection:wallet-a")
    assert first is not None
    assert store.claim_next("protection:wallet-a") is None
    store.mark_retry(first.event_id, "parse_timeout", delay_seconds=0)
    retry = store.claim_next("protection:wallet-a")
    assert retry is not None
    store.mark_processed(retry.event_id)

    second = store.claim_next("protection:wallet-a")
    assert second is not None
    assert store.checkpoint("protection:wallet-a") == (1, "signature-a")
    store.mark_processed(second.event_id)
    assert store.checkpoint("protection:wallet-a") == (2, "signature-b")


def test_history_replay_restores_missing_notifications_in_oldest_first_order(tmp_path):
    store = _store(tmp_path / "events.sqlite3")
    stream = "new-token:program"
    store.enqueue(stream, "checkpoint-signature", {}, slot=10)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    store.mark_processed(checkpoint.event_id)
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
    assert store.metrics()["replayed"] == 2
    assert not store.intake_paused(stream)
    first = store.claim_next(stream)
    assert first is not None and first.signature == "signature-b"
    store.mark_processed(first.event_id)
    second = store.claim_next(stream)
    assert second is not None and second.signature == "signature-c"


def test_history_replay_rejects_noncanonical_transaction_order(tmp_path):
    store = _store(tmp_path / "events.sqlite3")
    stream = "new-token:program"
    store.enqueue(stream, "checkpoint-signature", {}, slot=10)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    store.mark_processed(checkpoint.event_id)
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
    store.enqueue(stream, "checkpoint-signature", {}, slot=10)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    store.mark_processed(checkpoint.event_id)
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
    store.enqueue(stream, "checkpoint-signature", {}, slot=10)
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    store.mark_processed(checkpoint.event_id)
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


def test_history_replay_fails_closed_if_checkpoint_cannot_be_found(tmp_path):
    store = _store(tmp_path / "events.sqlite3")
    stream = "protection:wallet-a"
    store.enqueue(stream, "checkpoint-signature", {})
    event = store.claim_next(stream)
    assert event is not None
    store.mark_processed(event.event_id)
    store.mark_recovery_required(stream)

    async def missing_checkpoint(_address, _before, _limit):
        return [{"signature": "unrelated-signature", "slot": 20, "transactionIndex": 1}]

    with pytest.raises(EventRecoveryError, match="checkpoint"):
        asyncio.run(
            replay_stream_history(store, stream, "wallet-a", missing_checkpoint)
        )
    assert store.intake_paused(stream)
    assert store.recovery_required_streams("protection:") == [stream]


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


def test_fresh_stream_baseline_is_not_mistaken_for_a_recovery_gap(tmp_path):
    store = _store(tmp_path / "events.sqlite3")
    store.ensure_stream("new-token:program")

    async def handler(_event):
        raise AssertionError("fresh stream has no event to process")

    assert asyncio.run(
        recover_required_streams(
            store, "new-token:", handler, lambda name: name.removeprefix("new-token:")
        )
    )
    assert store.recovery_required_streams("new-token:") == []


def test_queue_saturation_is_recovered_from_readonly_history(tmp_path):
    store = _store(tmp_path / "events.sqlite3", max_pending_events=2)
    stream = "copytrade:wallet-a"
    store.enqueue(stream, "checkpoint-signature", {})
    checkpoint = store.claim_next(stream)
    assert checkpoint is not None
    store.mark_processed(checkpoint.event_id)
    store.enqueue(stream, "pending-a", {})
    store.enqueue(stream, "pending-b", {})
    with pytest.raises(EventQueueFullError):
        store.enqueue(stream, "missed-at-capacity", {})

    first = store.claim_next(stream)
    assert first is not None and first.signature == "pending-a"
    store.mark_processed(first.event_id)
    second = store.claim_next(stream)
    assert second is not None and second.signature == "pending-b"
    store.mark_processed(second.event_id)
    assert store.checkpoint(stream) == (3, "pending-b")

    async def fetch_page(_address, before, _limit):
        assert before is None
        return [
            {"signature": "missed-at-capacity", "slot": 13, "transactionIndex": 4},
            {"signature": "pending-b", "slot": 12, "transactionIndex": 3},
            {"signature": "pending-a", "slot": 11, "transactionIndex": 2},
            {"signature": "checkpoint-signature", "slot": 10, "transactionIndex": 1},
        ]

    assert asyncio.run(
        replay_stream_history(store, stream, "wallet-a", fetch_page)
    ) == 1
    replayed = store.claim_next(stream)
    assert replayed is not None and replayed.signature == "missed-at-capacity"
    store.mark_processed(replayed.event_id)
    assert store.checkpoint(stream) == (4, "missed-at-capacity")
    assert not store.intake_paused(stream)


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
    store.enqueue("copytrade:wallet-a", "signature-a", {})

    async def uncertain(_event):
        raise RuntimeError("downstream may have partially completed")

    assert asyncio.run(
        process_next_event(store, "copytrade:wallet-a", uncertain)
    ) == "unknown"
    assert store.checkpoint("copytrade:wallet-a") == (0, None)
    assert store.metrics()["unknown"] == 1
    assert store.claim_next("copytrade:wallet-a") is None
