from __future__ import annotations

from datetime import datetime, timedelta
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3

import pytest

from mission13_admission import PaperCandidateRequest
from mission13_market_policy import (
    MarketRegime,
    RegimeObservation,
    RegimePolicy,
    StrategyCandidate,
)
from mission13_scoring import ScoringProfile, SignalObservation, SignalRule
from mission18_paper_admission import (
    PaperCandidatePolicyEvaluation,
    PaperAdmissionConflict,
    PaperAdmissionStoreError,
    SQLitePaperAdmissionStore,
    evaluate_paper_candidate_policy_offline,
)
from sqlite_state import SCHEMA_VERSION, SQLiteStateStore


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "mission13_synthetic_paper_candidate.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
AS_OF = datetime.fromisoformat(FIXTURE["as_of"])


def _request(**updates) -> PaperCandidateRequest:
    observations = tuple(
        SignalObservation(
            name=row["name"],
            value=row["value"],
            observed_at=datetime.fromisoformat(row["observed_at"]),
            source=row["source"],
            provenance=row["provenance"],
        )
        for row in FIXTURE["observations"]
    )
    market = tuple(
        RegimeObservation(
            observed_at=datetime.fromisoformat(row["observed_at"]),
            close=row["close"],
            source=row["source"],
        )
        for row in FIXTURE["market_observations"]
    )
    profile = ScoringProfile(
        profile_id="synthetic-unvalidated-v1",
        rules=(
            SignalRule("liquidity_usd", 2, 0, 100, timedelta(minutes=1)),
            SignalRule("holder_concentration_pct", 1, 0, 100, timedelta(minutes=1), False),
            SignalRule(
                "mint_authority_active", 1, 0, 1, timedelta(minutes=1),
                False, True, block_above=0, block_reason_code="mint_authority_active",
            ),
        ),
        minimum_score=60,
    )
    strategy = StrategyCandidate(
        strategy_id=FIXTURE["strategy"]["strategy_id"],
        kind=FIXTURE["strategy"]["kind"],
        compatible_regimes=frozenset({MarketRegime.BULLISH_TREND}),
        required_signals=frozenset(FIXTURE["strategy"]["required_signals"]),
        priority=FIXTURE["strategy"]["priority"],
    )
    values = {
        "signal_id": FIXTURE["candidate"]["signal_id"],
        "correlation_key": FIXTURE["candidate"]["correlation_key"],
        "observations": observations,
        "scoring_profile": profile,
        "regime_observations": market,
        "regime_policy": RegimePolicy(
            min_observations=3,
            expected_interval=timedelta(minutes=1),
            max_latest_age=timedelta(minutes=5),
            max_observation_gap=timedelta(minutes=1),
            trend_threshold_pct=2,
            high_volatility_threshold_pct=8,
        ),
        "strategies": (strategy,),
    }
    values.update(updates)
    return PaperCandidateRequest(**values)


def _initialize_state(path: Path, *, emergency_stop: bool = False, losses: float = 0.0) -> None:
    SQLiteStateStore(path).save({
        "emergency_stop": emergency_stop,
        "session_losses_usd": losses,
        "open_positions": [],
    })


def _limits(**updates) -> dict:
    values = dict(FIXTURE["limits"])
    values.update(updates)
    return values


def _reserve_in_process(path, barrier, result_queue, signal_id, correlation_key):
    store = SQLitePaperAdmissionStore(path)
    barrier.wait(timeout=20)
    result_queue.put(store.reserve_entry(
        signal_id,
        correlation_key,
        10.0,
        max_exposure_usd=10.0,
        max_open_positions=1,
        max_session_loss_usd=50.0,
    ))


def _reserve_then_exit(path):
    store = SQLitePaperAdmissionStore(path)
    result = store.reserve_entry(
        "crash-signal",
        "crash-group",
        10.0,
        max_exposure_usd=10.0,
        max_open_positions=1,
        max_session_loss_usd=50.0,
    )
    os._exit(0 if result == (True, None) else 1)


def _commit_then_exit(path):
    store = SQLitePaperAdmissionStore(path)
    before = store.snapshot()
    if store.reserve_entry(
        "commit-crash-signal",
        "commit-crash-group",
        10.0,
        max_exposure_usd=100.0,
        max_open_positions=2,
        max_session_loss_usd=50.0,
    ) != (True, None):
        os._exit(1)
    store.commit_position(
        "commit-crash-signal",
        {
            **before.state,
            "open_positions": [{
                "mission18_signal_id": "commit-crash-signal",
                "token_mint": "synthetic-mint",
                "cost_basis_usd": 9.0,
                "units": 1.0,
            }],
        },
        expected_revision=before.revision,
    )
    # Simulate process loss after SQLite reported the atomic commit but before
    # any in-memory acknowledgement could run.
    os._exit(0)


def _reserve_exposure_in_process(path, barrier, result_queue, signal_id):
    store = SQLitePaperAdmissionStore(path)
    barrier.wait(timeout=20)
    result_queue.put(store.reserve_entry(
        signal_id,
        signal_id + "-group",
        10.0,
        max_exposure_usd=15.0,
        max_open_positions=10,
        max_session_loss_usd=50.0,
    ))


def test_synthetic_policy_result_cannot_be_used_as_runtime_admission(tmp_path):
    path = tmp_path / "state.sqlite3"
    _initialize_state(path)
    state = SQLiteStateStore(path).load(dict)
    limits = _limits()

    evaluation = evaluate_paper_candidate_policy_offline(
        _request(),
        mode="PAPER",
        as_of=AS_OF,
        position_state=state,
        proposed_exposure_usd=limits["proposed_exposure_usd"],
        configured_max_exposure_usd=limits["configured_max_exposure_usd"],
        configured_max_open_positions=limits["configured_max_open_positions"],
        configured_max_session_loss_usd=limits["configured_max_session_loss_usd"],
    )

    assert isinstance(evaluation, PaperCandidatePolicyEvaluation)
    assert evaluation.policy_decision.eligible is True
    assert evaluation.policy_decision.score == FIXTURE["expected"]["score"]
    assert evaluation.policy_decision.regime == FIXTURE["expected"]["regime"]
    assert evaluation.admission_status == "blocked"
    assert evaluation.provider_provenance_status == "UNKNOWN"
    assert evaluation.time_basis == "caller_supplied_unverified"
    assert evaluation.runtime_permitted is False
    assert evaluation.reservation_id is None
    assert not hasattr(evaluation, "eligible")
    assert SQLitePaperAdmissionStore(path).counts() == {
        "pending": 0, "unknown": 0, "committed": 0, "released": 0
    }


def test_duplicate_and_correlated_signals_are_refused_after_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    _initialize_state(path)
    first_store = SQLitePaperAdmissionStore(path)
    assert first_store.reserve_entry(
        FIXTURE["candidate"]["signal_id"],
        FIXTURE["candidate"]["correlation_key"],
        10.0,
        max_exposure_usd=100.0,
        max_open_positions=2,
        max_session_loss_usd=50.0,
    ) == (True, None)

    restarted = SQLitePaperAdmissionStore(path)
    duplicate = restarted.reserve_entry(
        FIXTURE["candidate"]["signal_id"],
        "synthetic-group-other",
        10.0,
        max_exposure_usd=100.0,
        max_open_positions=2,
        max_session_loss_usd=50.0,
    )
    correlated = restarted.reserve_entry(
        "synthetic-signal-0002",
        FIXTURE["candidate"]["correlation_key"],
        10.0,
        max_exposure_usd=100.0,
        max_open_positions=2,
        max_session_loss_usd=50.0,
    )

    assert duplicate == (False, "duplicate_signal")
    assert correlated == (False, "correlated_signal")
    assert restarted.counts()["pending"] == 1


@pytest.mark.parametrize(
    ("emergency_stop", "losses", "expected"),
    [(True, 0.0, "emergency_stop_active"), (False, 50.0, "session_loss_limit_reached")],
)
def test_persisted_stop_and_loss_limits_block_admission(tmp_path, emergency_stop, losses, expected):
    path = tmp_path / "state.sqlite3"
    _initialize_state(path, emergency_stop=emergency_stop, losses=losses)
    store = SQLitePaperAdmissionStore(path)

    result = store.reserve_entry(
        "signal-a",
        "group-a",
        10.0,
        max_exposure_usd=100.0,
        max_open_positions=2,
        max_session_loss_usd=50.0,
    )

    assert result == (False, expected)
    assert store.counts()["pending"] == 0


def test_distinct_processes_cannot_both_reserve_the_last_slot(tmp_path):
    path = tmp_path / "state.sqlite3"
    _initialize_state(path)
    SQLitePaperAdmissionStore(path)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    workers = [
        context.Process(
            target=_reserve_in_process,
            args=(path, barrier, results, f"signal-{index}", f"group-{index}"),
        )
        for index in range(2)
    ]
    for worker in workers:
        worker.start()
    outcomes = [results.get(timeout=30) for _ in workers]
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    assert sum(success for success, _ in outcomes) == 1
    assert sum(not success for success, _ in outcomes) == 1
    assert SQLitePaperAdmissionStore(path).counts()["pending"] == 1


def test_distinct_processes_cannot_exceed_exposure_limit_with_free_slots(tmp_path):
    path = tmp_path / "state.sqlite3"
    _initialize_state(path)
    SQLitePaperAdmissionStore(path)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    workers = [
        context.Process(
            target=_reserve_exposure_in_process,
            args=(path, barrier, results, f"exposure-signal-{index}"),
        )
        for index in range(2)
    ]
    for worker in workers:
        worker.start()
    outcomes = [results.get(timeout=30) for _ in workers]
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    assert sum(success for success, _ in outcomes) == 1
    assert sum(reason == "max_open_exposure_reached" for _, reason in outcomes) == 1
    assert SQLitePaperAdmissionStore(path).snapshot().reserved_exposure_usd == 10.0


def test_pending_and_unknown_reservations_survive_crash_and_stay_active(tmp_path):
    path = tmp_path / "state.sqlite3"
    _initialize_state(path)
    worker = multiprocessing.get_context("spawn").Process(
        target=_reserve_then_exit,
        args=(path,),
    )
    worker.start()
    worker.join(timeout=30)
    assert worker.exitcode == 0

    restarted = SQLitePaperAdmissionStore(path)
    assert restarted.snapshot().reserved_positions == 1
    assert restarted.mark_unknown("crash-signal") is True
    assert restarted.snapshot().reserved_positions == 1
    assert restarted.release_entry("crash-signal", effect_not_started=True) is False
    assert restarted.reserve_entry(
        "second-signal",
        "second-group",
        1.0,
        max_exposure_usd=100.0,
        max_open_positions=1,
        max_session_loss_usd=50.0,
    ) == (False, "max_open_positions_reached")
    assert restarted.counts()["unknown"] == 1


def test_position_and_reservation_commit_in_one_state_revision(tmp_path):
    path = tmp_path / "state.sqlite3"
    _initialize_state(path)
    store = SQLitePaperAdmissionStore(path)
    before = store.snapshot()
    assert store.reserve_entry(
        "signal-a",
        "group-a",
        10.0,
        max_exposure_usd=100.0,
        max_open_positions=2,
        max_session_loss_usd=50.0,
    ) == (True, None)

    after = dict(before.state)
    after["open_positions"] = [{
        "mission18_signal_id": "signal-a",
        "token_mint": "synthetic-mint",
        "cost_basis_usd": 9.0,
        "units": 1.0,
    }]
    revision = store.commit_position("signal-a", after, expected_revision=before.revision)

    persisted = SQLiteStateStore(path).load(dict)
    assert revision == before.revision + 1
    assert persisted["open_positions"] == after["open_positions"]
    assert store.counts() == {"pending": 0, "unknown": 0, "committed": 1, "released": 0}
    assert store.snapshot().reserved_positions == 0


def test_committed_position_survives_process_exit_and_replay_is_deduplicated(tmp_path):
    path = tmp_path / "state.sqlite3"
    _initialize_state(path)
    worker = multiprocessing.get_context("spawn").Process(
        target=_commit_then_exit,
        args=(path,),
    )
    worker.start()
    worker.join(timeout=30)
    assert worker.exitcode == 0

    restarted = SQLitePaperAdmissionStore(path)
    snapshot = restarted.snapshot()
    assert restarted.counts() == {
        "pending": 0, "unknown": 0, "committed": 1, "released": 0,
    }
    assert len(snapshot.state["open_positions"]) == 1
    assert snapshot.state["open_positions"][0]["mission18_signal_id"] == "commit-crash-signal"
    assert restarted.reserve_entry(
        "commit-crash-signal",
        "commit-crash-group",
        10.0,
        max_exposure_usd=100.0,
        max_open_positions=2,
        max_session_loss_usd=50.0,
    ) == (False, "duplicate_signal")
    assert len(restarted.snapshot().state["open_positions"]) == 1


def test_sqlite_failure_during_atomic_position_commit_keeps_old_state_and_pending_reservation(
    tmp_path,
):
    path = tmp_path / "state.sqlite3"
    _initialize_state(path)
    store = SQLitePaperAdmissionStore(path)
    before = store.snapshot()
    assert store.reserve_entry(
        "rollback-signal",
        "rollback-group",
        10.0,
        max_exposure_usd=100.0,
        max_open_positions=2,
        max_session_loss_usd=50.0,
    ) == (True, None)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_revision_update BEFORE UPDATE ON bot_state_revision "
            "BEGIN SELECT RAISE(ABORT, 'simulated_commit_failure'); END"
        )

    after = {
        **before.state,
        "open_positions": [{
            "mission18_signal_id": "rollback-signal",
            "token_mint": "synthetic-mint",
            "cost_basis_usd": 9.0,
            "units": 1.0,
        }],
    }
    with pytest.raises(PaperAdmissionStoreError, match="position_commit_failed"):
        store.commit_position("rollback-signal", after, expected_revision=before.revision)

    restarted = SQLitePaperAdmissionStore(path)
    snapshot = restarted.snapshot()
    assert snapshot.state["open_positions"] == []
    assert snapshot.revision == before.revision
    assert restarted.counts() == {
        "pending": 1, "unknown": 0, "committed": 0, "released": 0,
    }


def test_state_revision_conflict_keeps_reservation_pending(tmp_path):
    path = tmp_path / "state.sqlite3"
    _initialize_state(path)
    store = SQLitePaperAdmissionStore(path)
    before = store.snapshot()
    assert store.reserve_entry(
        "signal-a",
        "group-a",
        10.0,
        max_exposure_usd=100.0,
        max_open_positions=2,
        max_session_loss_usd=50.0,
    ) == (True, None)
    SQLiteStateStore(path).save({
        **before.state,
        "operator_note": "state changed after reservation",
    })

    with pytest.raises(PaperAdmissionConflict, match="revision_changed"):
        store.commit_position(
            "signal-a",
            {
                **before.state,
                "open_positions": [{
                    "mission18_signal_id": "signal-a",
                    "cost_basis_usd": 1.0,
                    "units": 1.0,
                }],
            },
            expected_revision=before.revision,
        )

    assert store.counts()["pending"] == 1
    assert store.snapshot().reserved_positions == 1


def test_caller_assertion_cannot_release_a_durable_reservation(tmp_path):
    path = tmp_path / "state.sqlite3"
    _initialize_state(path)
    store = SQLitePaperAdmissionStore(path)
    assert store.reserve_entry(
        "signal-a",
        "group-a",
        10.0,
        max_exposure_usd=100.0,
        max_open_positions=2,
        max_session_loss_usd=50.0,
    ) == (True, None)

    assert store.release_entry("signal-a", effect_not_started=False) is False
    assert store.release_entry("signal-a", effect_not_started=True) is False
    assert store.reserve_entry(
        "signal-a",
        "group-a",
        10.0,
        max_exposure_usd=100.0,
        max_open_positions=2,
        max_session_loss_usd=50.0,
    ) == (False, "duplicate_signal")
    assert store.snapshot().reserved_positions == 1


def test_live_and_invalid_snapshot_inputs_are_refused(tmp_path):
    path = tmp_path / "state.sqlite3"
    _initialize_state(path)
    store = SQLitePaperAdmissionStore(path)
    limits = _limits()
    evaluation = evaluate_paper_candidate_policy_offline(
        _request(),
        mode="LIVE",
        as_of=AS_OF,
        position_state=SQLiteStateStore(path).load(dict),
        proposed_exposure_usd=limits["proposed_exposure_usd"],
        configured_max_exposure_usd=limits["configured_max_exposure_usd"],
        configured_max_open_positions=limits["configured_max_open_positions"],
        configured_max_session_loss_usd=limits["configured_max_session_loss_usd"],
    )

    assert evaluation.policy_decision.status == "rejected"
    assert evaluation.policy_decision.reason_codes == ("paper_mode_required",)
    assert evaluation.runtime_permitted is False
    assert store.counts()["pending"] == 0

    with pytest.raises(PaperAdmissionStoreError, match="state_schema_unavailable"):
        SQLitePaperAdmissionStore(tmp_path / "missing-state.sqlite3").snapshot()


@pytest.mark.parametrize("version_location", ["row", "pragma"])
def test_future_sqlite_state_schema_blocks_snapshot_reserve_and_commit(
    tmp_path, version_location
):
    path = tmp_path / "state.sqlite3"
    _initialize_state(path)
    store = SQLitePaperAdmissionStore(path)
    before = store.snapshot()
    assert store.reserve_entry(
        "schema-signal",
        "schema-group",
        10.0,
        max_exposure_usd=100.0,
        max_open_positions=2,
        max_session_loss_usd=50.0,
    ) == (True, None)

    with sqlite3.connect(path) as connection:
        if version_location == "row":
            connection.execute(
                "UPDATE bot_state SET schema_version=? WHERE singleton=1",
                (SCHEMA_VERSION + 1,),
            )
        else:
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")

    with pytest.raises(PaperAdmissionStoreError, match="state_schema_unsupported"):
        store.snapshot()
    assert store.reserve_entry(
        "other-signal",
        "other-group",
        1.0,
        max_exposure_usd=100.0,
        max_open_positions=2,
        max_session_loss_usd=50.0,
    ) == (False, "paper_state_schema_unsupported")
    with pytest.raises(PaperAdmissionStoreError, match="state_schema_unsupported"):
        store.commit_position(
            "schema-signal",
            {
                **before.state,
                "open_positions": [{
                    "mission18_signal_id": "schema-signal",
                    "cost_basis_usd": 1.0,
                    "units": 1.0,
                }],
            },
            expected_revision=before.revision,
        )

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT payload FROM bot_state WHERE singleton=1"
        ).fetchone()
        revision = connection.execute(
            "SELECT revision FROM bot_state_revision WHERE singleton=1"
        ).fetchone()[0]
    assert row[0] == SQLiteStateStore._encode_state(before.state)
    assert revision == before.revision
    assert store.counts()["pending"] == 1


@pytest.mark.parametrize("version_location", ["row", "pragma"])
def test_constructor_does_not_modify_a_future_schema_database(tmp_path, version_location):
    path = tmp_path / "future-state.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE bot_state (
                singleton INTEGER PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE bot_state_revision (
                singleton INTEGER PRIMARY KEY,
                revision INTEGER NOT NULL
            );
            CREATE TABLE unrelated_marker (value TEXT NOT NULL);
            INSERT INTO bot_state VALUES (1, 1, '{}', 'now');
            INSERT INTO bot_state_revision VALUES (1, 0);
            INSERT INTO unrelated_marker VALUES ('preserve');
            """
        )
        if version_location == "row":
            connection.execute(
                "UPDATE bot_state SET schema_version=? WHERE singleton=1",
                (SCHEMA_VERSION + 1,),
            )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        else:
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")

    with sqlite3.connect(path) as connection:
        tables_before = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        journal_mode_before = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert journal_mode_before == "delete"

    with pytest.raises(PaperAdmissionStoreError, match="state_schema_unsupported"):
        SQLitePaperAdmissionStore(path)

    with sqlite3.connect(path) as connection:
        tables_after = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        journal_mode_after = connection.execute("PRAGMA journal_mode").fetchone()[0]
        marker = connection.execute("SELECT value FROM unrelated_marker").fetchone()[0]
    assert tables_after == tables_before
    assert "mission18_paper_reservations" not in tables_after
    assert journal_mode_after == journal_mode_before
    assert marker == "preserve"


def test_emergency_stop_blocks_commit_even_with_fresh_revision(tmp_path):
    path = tmp_path / "state.sqlite3"
    _initialize_state(path)
    store = SQLitePaperAdmissionStore(path)
    assert store.reserve_entry(
        "halt-signal",
        "halt-group",
        10.0,
        max_exposure_usd=100.0,
        max_open_positions=2,
        max_session_loss_usd=50.0,
    ) == (True, None)

    SQLiteStateStore(path).save({
        "emergency_stop": True,
        "session_losses_usd": 0.0,
        "open_positions": [],
    })
    stopped = store.snapshot()
    with pytest.raises(PaperAdmissionStoreError, match="emergency_stop_active"):
        store.commit_position(
            "halt-signal",
            {
                **stopped.state,
                "open_positions": [{
                    "mission18_signal_id": "halt-signal",
                    "cost_basis_usd": 1.0,
                    "units": 1.0,
                }],
            },
            expected_revision=stopped.revision,
        )
    assert store.counts()["pending"] == 1
    assert SQLiteStateStore(path).load(dict)["open_positions"] == []
