import multiprocessing

import pytest

from mission13_signal_store import SQLitePaperSignalLedger


def _reserve_signal_in_process(path, barrier, result_queue, signal_id, correlation_key):
    ledger = SQLitePaperSignalLedger(path)
    barrier.wait(timeout=20)
    result_queue.put(ledger.reserve(signal_id, correlation_key))


def test_sqlite_signal_ledger_persists_committed_and_blocks_replay(tmp_path):
    path = tmp_path / "paper_signals.sqlite3"
    ledger = SQLitePaperSignalLedger(path)
    assert ledger.reserve("fixture-signal", "fixture-group") == (True, None)
    assert ledger.commit("fixture-signal") is True

    restarted = SQLitePaperSignalLedger(path)
    assert restarted.snapshot() == (("committed", "fixture-signal", "fixture-group"),)
    assert restarted.reserve("fixture-signal", "new-group") == (False, "duplicate_signal")
    assert restarted.reserve("new-signal", "fixture-group") == (False, "correlated_signal")
    assert restarted.release("fixture-signal") is False


def test_sqlite_signal_ledger_releases_only_uncommitted_reservations(tmp_path):
    ledger = SQLitePaperSignalLedger(tmp_path / "paper_signals.sqlite3")
    assert ledger.reserve("pending-signal", "pending-group") == (True, None)
    assert ledger.release("pending-signal") is True
    assert ledger.reserve("pending-signal", "pending-group") == (True, None)
    assert ledger.commit("pending-signal") is True
    assert ledger.release("pending-signal") is False


def test_sqlite_signal_ledger_rejects_conflicting_restart_seed(tmp_path):
    path = tmp_path / "paper_signals.sqlite3"
    ledger = SQLitePaperSignalLedger(path)
    assert ledger.reserve("fixture-signal", "fixture-group") == (True, None)

    with pytest.raises(RuntimeError, match="paper_signal_ledger_seed_conflict"):
        SQLitePaperSignalLedger(path, committed=(("fixture-signal", "other-group"),))

    with pytest.raises(RuntimeError, match="paper_signal_ledger_seed_conflict"):
        SQLitePaperSignalLedger(path, committed=(("other-signal", "fixture-group"),))


def test_sqlite_signal_ledger_reserves_shared_correlation_once_across_processes(tmp_path):
    path = tmp_path / "paper_signals.sqlite3"
    SQLitePaperSignalLedger(path)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    workers = [
        context.Process(
            target=_reserve_signal_in_process,
            args=(path, barrier, results, f"fixture-signal-{index}", "shared-group"),
        )
        for index in range(2)
    ]
    for worker in workers:
        worker.start()

    outcomes = [results.get(timeout=30) for _ in workers]
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    assert sum(1 for success, _ in outcomes if success) == 1
    assert sum(1 for success, reason in outcomes if not success and reason == "correlated_signal") == 1
    assert len(SQLitePaperSignalLedger(path).snapshot()) == 1
