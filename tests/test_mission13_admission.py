import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mission13_admission import (
    PaperCandidateRequest,
    PaperSignalLedger,
    evaluate_paper_candidate,
)
from mission13_market_policy import (
    MarketRegime,
    RegimeObservation,
    RegimePolicy,
    StrategyCandidate,
)
from mission13_scoring import (
    ScoringProfile,
    SignalObservation,
    SignalRule,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "mission13_synthetic_paper_candidate.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8-sig"))
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


def _evaluate(
    request=None,
    *,
    mode="PAPER",
    as_of=AS_OF,
    position_state=None,
    signal_ledger=None,
    **limit_updates,
):
    limits = dict(FIXTURE["limits"])
    limits.update(limit_updates)
    reserved_exposure_usd = limits.pop('reserved_exposure_usd', 0.0)
    reserved_positions = limits.pop('reserved_positions', 0)
    state = dict(FIXTURE["risk_state"]) if position_state is None else position_state
    return evaluate_paper_candidate(
        request or _request(),
        mode=mode,
        as_of=as_of,
        position_state=state,
        signal_ledger=signal_ledger or PaperSignalLedger(),
        reserved_exposure_usd=reserved_exposure_usd,
        reserved_positions=reserved_positions,
        **limits,
    )


def test_synthetic_fixture_composes_scoring_regime_strategy_and_risk():
    decision = _evaluate()

    assert decision.status == FIXTURE["expected"]["status"]
    assert decision.eligible is True
    assert decision.score == FIXTURE["expected"]["score"]
    assert decision.profile_id == "synthetic-unvalidated-v1"
    assert decision.regime == FIXTURE["expected"]["regime"]
    assert decision.strategy_id == FIXTURE["expected"]["strategy_id"]
    assert decision.reservation_id == FIXTURE["candidate"]["signal_id"]
    assert decision.reason_codes == (
        "paper_mode", "score_eligible", "regime_classified",
        "strategy_selected", "risk_limits_passed",
    )
    assert {row.source for row in decision.contributions} == {"synthetic_fixture"}


def test_live_mode_is_refused_before_any_reservation():
    ledger = PaperSignalLedger()
    decision = _evaluate(mode="LIVE", signal_ledger=ledger)

    assert decision.status == "rejected"
    assert decision.reason_codes == ("paper_mode_required",)
    assert ledger.snapshot() == ()


def test_missing_or_stale_evidence_fails_closed_without_reservation():
    ledger = PaperSignalLedger()
    incomplete = _request(observations=_request().observations[:-1])
    decision = _evaluate(incomplete, signal_ledger=ledger)

    assert decision.status == "rejected"
    assert "missing_required_signal" in decision.reason_codes
    assert decision.score is None
    assert ledger.snapshot() == ()

    stale_time = AS_OF + timedelta(minutes=6)
    stale = _evaluate(as_of=stale_time, signal_ledger=ledger)
    assert stale.status == "rejected"
    assert "stale_signal" in stale.reason_codes
    assert ledger.snapshot() == ()


def test_unknown_regime_or_unregistered_strategy_fails_closed():
    request = _request(
        strategies=(
            StrategyCandidate(
                "bear-only",
                "rule",
                frozenset({MarketRegime.BEARISH_TREND}),
                frozenset({"liquidity_usd"}),
            ),
        ),
    )
    decision = _evaluate(request)

    assert decision.status == "rejected"
    assert decision.regime == "bullish_trend"
    assert decision.reason_codes == ("no_eligible_strategy",)


@pytest.mark.parametrize(
    ("state", "expected_reason"),
    [
        (
            {"emergency_stop": True, "session_losses_usd": 0, "open_positions": []},
            "emergency_stop_active",
        ),
        (
            {"emergency_stop": False, "session_losses_usd": 50, "open_positions": []},
            "session_loss_limit_reached",
        ),
        (
            {"emergency_stop": "false", "session_losses_usd": 0, "open_positions": []},
            "invalid_emergency_stop_state",
        ),
        (
            {"emergency_stop": False, "session_losses_usd": float("nan"), "open_positions": []},
            "invalid_session_loss_state_or_limit",
        ),
    ],
)
def test_existing_loss_and_emergency_guards_fail_closed(state, expected_reason):
    decision = _evaluate(position_state=state)

    assert decision.status == "rejected"
    assert decision.reason_codes == (expected_reason,)


def test_existing_exposure_and_position_limits_are_reused():
    exposure = _evaluate(configured_max_exposure_usd=9)
    positions = _evaluate(
        position_state={
            "emergency_stop": False,
            "session_losses_usd": 0,
            "open_positions": [{"cost_basis_usd": 95}],
        },
        configured_max_exposure_usd=100,
    )

    assert exposure.reason_codes == ("max_open_exposure_reached",)
    assert positions.reason_codes == ("max_open_exposure_reached",)


def test_signal_ledger_blocks_duplicate_and_correlated_candidates():
    ledger = PaperSignalLedger()
    accepted = _evaluate(signal_ledger=ledger)
    assert accepted.eligible
    assert ledger.commit(accepted.reservation_id)

    duplicate = _evaluate(signal_ledger=ledger)
    correlated = _evaluate(
        _request(
            signal_id="synthetic-signal-0002",
            correlation_key=FIXTURE["candidate"]["correlation_key"],
        ),
        signal_ledger=ledger,
    )

    assert duplicate.reason_codes == ("duplicate_signal",)
    assert correlated.reason_codes == ("correlated_signal",)


def test_signal_ledger_supports_atomic_pending_reservations_and_release():
    ledger = PaperSignalLedger()
    first = ledger.reserve("signal-a", "group-a")
    racing_duplicate = ledger.reserve("signal-a", "group-b")
    correlated = ledger.reserve("signal-b", "group-a")

    assert first == (True, None)
    assert racing_duplicate == (False, "duplicate_signal")
    assert correlated == (False, "correlated_signal")
    assert ledger.snapshot() == (("pending", "signal-a", "group-a"),)
    assert ledger.release("signal-a")
    assert ledger.snapshot() == ()
    assert ledger.reserve("signal-c", "group-c") == (True, None)
    assert ledger.commit("signal-c")
    assert ledger.snapshot() == (("committed", "signal-c", "group-c"),)


def test_missing_reservation_snapshot_fails_closed():
    decision = _evaluate(reserved_exposure_usd=None)

    assert decision.reason_codes == ("invalid_reserved_risk_snapshot",)

def test_invalid_committed_ledger_fails_closed_at_construction():
    with pytest.raises(ValueError, match="duplicate_or_correlated_committed_signal"):
        PaperSignalLedger((("signal-a", "group-a"), ("signal-b", "group-a")))
