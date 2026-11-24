from datetime import datetime, timedelta, timezone

import pytest

from mission13_market_policy import (
    MarketRegime,
    RegimeAssessment,
    RegimeObservation,
    RegimePolicy,
    StrategyCandidate,
    classify_market_regime,
    select_paper_strategies,
)


BASE = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
POLICY = RegimePolicy(
    min_observations=3,
    expected_interval=timedelta(minutes=1),
    max_latest_age=timedelta(minutes=10),
    max_observation_gap=timedelta(minutes=3),
    trend_threshold_pct=2.0,
    high_volatility_threshold_pct=8.0,
)


def _series(prices, *, timestamps=None, source="synthetic_fixture"):
    timestamps = timestamps or [BASE + timedelta(minutes=index) for index in range(len(prices))]
    return [
        RegimeObservation(observed_at=timestamp, close=price, source=source)
        for timestamp, price in zip(timestamps, prices)
    ]


@pytest.mark.parametrize(
    "prices,expected",
    [
        ([100, 103, 106], MarketRegime.BULLISH_TREND),
        ([100, 97, 94], MarketRegime.BEARISH_TREND),
        ([100, 100.2, 100.1], MarketRegime.SIDEWAYS),
        ([100, 130, 90, 120], MarketRegime.HIGH_VOLATILITY),
    ],
)
def test_classifies_synthetic_regimes_explainably(prices, expected):
    observations = _series(prices)

    result = classify_market_regime(
        observations,
        as_of=observations[-1].observed_at,
        policy=POLICY,
    )

    assert result.regime is expected
    assert result.ready is True
    assert result.reason_code
    assert result.observation_count == len(prices)
    assert result.sources == ("synthetic_fixture",)
    assert result.trend_pct is not None
    assert result.volatility_pct is not None


def test_rejects_insufficient_or_empty_series():
    empty = classify_market_regime([], as_of=BASE, policy=POLICY)
    short = classify_market_regime(_series([100, 101]), as_of=BASE, policy=POLICY)

    assert (empty.regime, empty.reason_code) == (MarketRegime.UNKNOWN, "insufficient_observations")
    assert (short.regime, short.reason_code) == (MarketRegime.UNKNOWN, "insufficient_observations")


def test_rejects_stale_series():
    observations = _series(
        [100, 101, 102],
        timestamps=[BASE, BASE + timedelta(minutes=1), BASE + timedelta(minutes=2)],
    )

    result = classify_market_regime(
        observations,
        as_of=BASE + timedelta(minutes=13),
        policy=POLICY,
    )

    assert (result.regime, result.reason_code) == (MarketRegime.UNKNOWN, "stale_latest_observation")


def test_rejects_future_naive_out_of_order_and_sparse_observations():
    valid_prefix = _series([100, 101])

    future = valid_prefix + [
        RegimeObservation(BASE + timedelta(minutes=3), 102, "fixture")
    ]
    result = classify_market_regime(future, as_of=BASE + timedelta(minutes=2), policy=POLICY)
    assert result.reason_code == "future_observation"

    naive = _series([100, 101, 102])
    naive[1] = RegimeObservation(datetime(2026, 11, 24, 12, 1), 101, "fixture")
    result = classify_market_regime(naive, as_of=BASE + timedelta(minutes=2), policy=POLICY)
    assert result.reason_code == "invalid_observation_timestamp"

    unordered = _series(
        [100, 101, 102],
        timestamps=[BASE, BASE + timedelta(minutes=2), BASE + timedelta(minutes=1)],
    )
    result = classify_market_regime(unordered, as_of=BASE + timedelta(minutes=2), policy=POLICY)
    assert result.reason_code == "observations_not_strictly_increasing"

    sparse = _series(
        [100, 101, 102],
        timestamps=[BASE, BASE + timedelta(minutes=1), BASE + timedelta(minutes=6)],
    )
    result = classify_market_regime(sparse, as_of=BASE + timedelta(minutes=6), policy=POLICY)
    assert result.reason_code == "observation_gap_exceeded"


@pytest.mark.parametrize(
    "row,reason",
    [
        (RegimeObservation(BASE + timedelta(minutes=1), 0, "fixture"), "invalid_close_price"),
        (RegimeObservation(BASE + timedelta(minutes=1), float("nan"), "fixture"), "invalid_close_price"),
        (RegimeObservation(BASE + timedelta(minutes=1), True, "fixture"), "invalid_close_price"),
        (RegimeObservation(BASE + timedelta(minutes=1), 100, " "), "missing_source_provenance"),
    ],
)
def test_rejects_malformed_observations(row, reason):
    observations = _series([100, 101, 102])
    observations[1] = row

    result = classify_market_regime(observations, as_of=BASE + timedelta(minutes=2), policy=POLICY)

    assert (result.regime, result.reason_code) == (MarketRegime.UNKNOWN, reason)


def test_rejects_irregular_sampling_even_when_gap_is_small():
    observations = _series(
        [100, 101, 102],
        timestamps=[BASE, BASE + timedelta(seconds=30), BASE + timedelta(minutes=1)],
    )

    result = classify_market_regime(observations, as_of=BASE + timedelta(minutes=1), policy=POLICY)

    assert result.reason_code == "inconsistent_observation_interval"

def test_invalid_policy_or_as_of_fails_closed():
    result = classify_market_regime(
        _series([100, 101, 102]),
        as_of=datetime(2026, 11, 24, 12),
        policy=POLICY,
    )
    assert result.reason_code == "invalid_as_of"

    bad_policy = RegimePolicy(1, timedelta(minutes=1), timedelta(minutes=1), timedelta(minutes=1), 1, 1)
    result = classify_market_regime(_series([100, 101, 102]), as_of=BASE, policy=bad_policy)
    assert result.reason_code == "invalid_policy"


def test_selects_only_registered_compatible_paper_modes_with_required_inputs():
    observations = _series([100, 103, 106])
    regime = classify_market_regime(observations, as_of=observations[-1].observed_at, policy=POLICY)
    candidates = [
        StrategyCandidate(
            "momentum_rule",
            "rule",
            frozenset({MarketRegime.BULLISH_TREND}),
            frozenset({"price_history"}),
            priority=1,
        ),
        StrategyCandidate(
            "existing_copy_mode",
            "copy_trading_existing",
            frozenset({MarketRegime.BULLISH_TREND}),
            frozenset({"wallet_event"}),
        ),
        StrategyCandidate(
            "existing_snipe_mode",
            "sniping_existing",
            frozenset({MarketRegime.BEARISH_TREND}),
            frozenset({"creation_event"}),
        ),
        StrategyCandidate(
            "missing_input_rule",
            "rule",
            frozenset({MarketRegime.BULLISH_TREND}),
            frozenset({"holder_distribution"}),
        ),
    ]

    result = select_paper_strategies(
        regime,
        candidates,
        available_signals={"price_history", "wallet_event"},
        mode="PAPER",
    )

    assert result.accepted is True
    assert result.status == "eligible"
    assert result.strategy_id == "momentum_rule"
    assert result.regime is MarketRegime.BULLISH_TREND
    assert result.reason_codes == ("paper_mode", "regime_compatible", "required_signals_present")
    assert result.selected_strategy_ids == ("momentum_rule",)
    assert result.rejected == (
        ("existing_snipe_mode", "regime_incompatible"),
        ("missing_input_rule", "required_signal_missing"),
    )
    assert result.mode == "PAPER"


def test_strategy_selection_fails_closed_for_unknown_regime_live_or_invalid_registry():
    valid = RegimeAssessment(
        MarketRegime.BULLISH_TREND,
        "test",
        BASE,
        3,
        ("fixture",),
        trend_pct=3,
        volatility_pct=1,
    )
    candidate = StrategyCandidate(
        "rule", "rule", frozenset({MarketRegime.BULLISH_TREND}), frozenset()
    )

    live = select_paper_strategies(valid, [candidate], available_signals=set(), mode="LIVE")
    assert (live.accepted, live.reason_code) == (False, "paper_mode_required")

    unknown = RegimeAssessment(MarketRegime.UNKNOWN, "missing_data", BASE, 0, ())
    blocked = select_paper_strategies(unknown, [candidate], available_signals=set(), mode="PAPER")
    assert (blocked.accepted, blocked.reason_code) == (False, "market_regime_unavailable")

    duplicate = select_paper_strategies(
        valid,
        [candidate, candidate],
        available_signals=set(),
        mode="PAPER",
    )
    assert (duplicate.accepted, duplicate.reason_code) == (False, "duplicate_strategy_id")

    no_match = select_paper_strategies(
        valid,
        [
            StrategyCandidate(
                "wrong_kind", "unknown", frozenset({MarketRegime.BULLISH_TREND}), frozenset()
            )
        ],
        available_signals=set(),
        mode="PAPER",
    )
    assert (no_match.accepted, no_match.reason_code) == (False, "invalid_strategy_registration")


def test_strategy_selection_rejects_manually_incomplete_regime_assessment():
    incomplete = RegimeAssessment(
        MarketRegime.BULLISH_TREND, "made_up", None, 3, (), None, None
    )
    candidate = StrategyCandidate(
        "rule", "rule", frozenset({MarketRegime.BULLISH_TREND}), frozenset()
    )

    result = select_paper_strategies(
        incomplete, [candidate], available_signals=set(), mode="PAPER"
    )

    assert incomplete.ready is False
    assert result.reason_code == "market_regime_unavailable"

def test_existing_sniping_and_copy_modes_are_identifiers_only():
    assessment = RegimeAssessment(
        MarketRegime.SIDEWAYS,
        "synthetic_case",
        BASE,
        3,
        ("fixture",),
        trend_pct=0.0,
        volatility_pct=0.0,
    )
    candidates = [
        StrategyCandidate("copy", "copy_trading_existing", frozenset({MarketRegime.SIDEWAYS}), frozenset()),
        StrategyCandidate("snipe", "sniping_existing", frozenset({MarketRegime.SIDEWAYS}), frozenset()),
    ]

    result = select_paper_strategies(
        assessment,
        candidates,
        available_signals=set(),
        mode="PAPER",
    )

    assert result.selected_strategy_ids == ("copy",)
    assert "execute" not in result.reason_code
