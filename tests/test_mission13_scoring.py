from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from mission13_scoring import (
    ScoringProfile,
    SignalObservation,
    SignalRule,
    score_token_evidence,
)


AS_OF = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def _profile() -> ScoringProfile:
    """Return an explicitly synthetic, unvalidated test profile."""
    return ScoringProfile(
        profile_id="synthetic-test-v1",
        rules=(
            SignalRule(
                name="liquidity_usd",
                weight=2,
                minimum=0,
                maximum=100,
                max_age=timedelta(seconds=60),
            ),
            SignalRule(
                name="holder_concentration_pct",
                weight=1,
                minimum=0,
                maximum=100,
                max_age=timedelta(seconds=30),
                higher_is_better=False,
            ),
            SignalRule(
                name="mint_authority_active",
                weight=1,
                minimum=0,
                maximum=1,
                max_age=timedelta(seconds=60),
                higher_is_better=False,
                allow_boolean=True,
                block_above=0,
                block_reason_code="mint_authority_active",
            ),
        ),
        minimum_score=60,
    )


def _observations(
    *,
    liquidity: int | float | bool = 50,
    concentration: int | float | bool = 20,
    mint_authority: int | float | bool = False,
    observed_at: datetime = AS_OF,
) -> list[SignalObservation]:
    return [
        SignalObservation(
            name="liquidity_usd",
            value=liquidity,
            observed_at=observed_at,
            source="synthetic_market_fixture",
            provenance="fixture field liquidity_usd",
        ),
        SignalObservation(
            name="holder_concentration_pct",
            value=concentration,
            observed_at=observed_at,
            source="synthetic_holder_fixture",
            provenance="fixture field holder_concentration_pct",
        ),
        SignalObservation(
            name="mint_authority_active",
            value=mint_authority,
            observed_at=observed_at,
            source="synthetic_security_fixture",
            provenance="fixture field mint_authority_active",
        ),
    ]


def test_scores_with_explainable_weighted_contributions():
    result = score_token_evidence(_observations(), profile=_profile(), as_of=AS_OF)

    assert result.status == "eligible"
    assert result.eligible is True
    assert result.score == 70
    assert result.reason_codes == ()
    assert [item.name for item in result.contributions] == [
        "liquidity_usd",
        "holder_concentration_pct",
        "mint_authority_active",
    ]
    assert [item.weighted_points for item in result.contributions] == [25, 20, 25]
    assert all(item.source.startswith("synthetic_") for item in result.contributions)
    assert all(item.provenance for item in result.contributions)
    assert result.scored_at == AS_OF


def test_below_threshold_is_rejected_with_calculated_explanation():
    result = score_token_evidence(
        _observations(liquidity=0, concentration=100),
        profile=_profile(),
        as_of=AS_OF,
    )

    assert result.status == "rejected"
    assert result.eligible is False
    assert result.score == 25
    assert result.reason_codes == ("score_below_profile_threshold",)
    assert len(result.contributions) == 3


def test_configured_fraud_indicator_hard_blocks_candidate():
    result = score_token_evidence(
        _observations(mint_authority=True), profile=_profile(), as_of=AS_OF
    )

    assert result.status == "rejected"
    assert result.score == 45
    assert result.reason_codes == (
        "mint_authority_active",
        "score_below_profile_threshold",
    )


def test_configured_liquidity_drawdown_indicator_blocks_without_claiming_detection():
    profile = ScoringProfile(
        profile_id="synthetic-risk-indicator-v1",
        rules=(
            SignalRule(
                name="liquidity_drawdown_pct",
                weight=1,
                minimum=0,
                maximum=100,
                max_age=timedelta(minutes=1),
                higher_is_better=False,
                block_above=30,
                block_reason_code="configured_liquidity_drawdown_block",
            ),
        ),
        minimum_score=0,
    )
    observation = SignalObservation(
        name="liquidity_drawdown_pct",
        value=40,
        observed_at=AS_OF,
        source="synthetic_fixture",
        provenance="fixture indicator; calculation belongs to upstream provider",
    )

    result = score_token_evidence([observation], profile=profile, as_of=AS_OF)

    assert result.status == "rejected"
    assert result.reason_codes == ("configured_liquidity_drawdown_block",)
    assert result.score == 60

def test_missing_required_evidence_has_no_score():
    result = score_token_evidence(
        _observations()[:-1], profile=_profile(), as_of=AS_OF
    )

    assert result.status == "rejected"
    assert result.score is None
    assert "missing_required_signal" in result.reason_codes
    assert result.contributions == ()


@pytest.mark.parametrize(
    ("timestamp", "reason"),
    [
        (AS_OF - timedelta(seconds=61), "stale_signal"),
        (AS_OF + timedelta(seconds=1), "future_signal_timestamp"),
        (datetime(2026, 11, 24, 12, 0), "invalid_signal_timestamp"),
    ],
)
def test_invalid_timestamps_fail_closed(timestamp, reason):
    result = score_token_evidence(
        _observations(observed_at=timestamp), profile=_profile(), as_of=AS_OF
    )

    assert result.status == "rejected"
    assert result.score is None
    assert reason in result.reason_codes


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (float("nan"), "invalid_signal_value"),
        (float("inf"), "invalid_signal_value"),
        ("50", "invalid_signal_value"),
        (True, "boolean_signal_not_allowed"),
        (-1, "signal_value_out_of_range"),
        (101, "signal_value_out_of_range"),
    ],
)
def test_malformed_and_out_of_range_values_fail_closed(value, reason):
    result = score_token_evidence(
        _observations(liquidity=value), profile=_profile(), as_of=AS_OF
    )

    assert result.status == "rejected"
    assert result.score is None
    assert reason in result.reason_codes


def test_missing_provenance_and_unknown_signal_fail_closed():
    observations = _observations()
    observations[0] = replace(observations[0], source=" ", provenance=" ")
    observations.append(
        SignalObservation(
            name="unconfigured_signal",
            value=1,
            observed_at=AS_OF,
            source="fixture",
            provenance="fixture field",
        )
    )

    result = score_token_evidence(observations, profile=_profile(), as_of=AS_OF)

    assert result.status == "rejected"
    assert result.score is None
    assert "missing_signal_source" in result.reason_codes
    assert "missing_signal_provenance" in result.reason_codes
    assert "unknown_signal" in result.reason_codes


def test_conflicting_duplicate_is_rejected_but_exact_repeat_is_deduplicated():
    observations = _observations()
    duplicate = observations[0]
    exact_repeat = score_token_evidence(
        [*observations, duplicate], profile=_profile(), as_of=AS_OF
    )
    conflicting = score_token_evidence(
        [*observations, replace(duplicate, value=51)],
        profile=_profile(),
        as_of=AS_OF,
    )

    assert exact_repeat.status == "eligible"
    assert len(exact_repeat.contributions) == 3
    assert conflicting.status == "rejected"
    assert conflicting.score is None
    assert "conflicting_duplicate_signal" in conflicting.reason_codes


def test_profile_rejects_ambiguous_or_invalid_rules():
    rule = _profile().rules[0]
    with pytest.raises(ValueError, match="scoring_profile_duplicate_rule"):
        ScoringProfile("duplicate-v1", (rule, rule), minimum_score=50)

    with pytest.raises(ValueError, match="signal_rule_range_invalid"):
        SignalRule(
            name="bad_range",
            weight=1,
            minimum=10,
            maximum=10,
            max_age=timedelta(seconds=1),
        )


def test_naive_scoring_time_is_rejected_as_bad_call_configuration():
    with pytest.raises(ValueError, match="scoring_as_of_must_be_timezone_aware"):
        score_token_evidence(
            _observations(), profile=_profile(), as_of=datetime(2026, 11, 24, 12)
        )
