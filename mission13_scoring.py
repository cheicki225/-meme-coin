"""Deterministic, explainable scoring for timestamped token evidence.

This module evaluates caller-supplied evidence only. It does not fetch market
data, identify scams, or establish that a token is safe. A profile is a
heuristic policy whose weights and ranges must be supplied and reviewed by
the caller; no profile is validated against real market outcomes here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence


def _is_aware(value: datetime) -> bool:
    """Return whether a datetime has a usable UTC offset."""
    return value.tzinfo is not None and value.utcoffset() is not None


def _finite_number(value: object) -> bool:
    """Accept finite numeric values while excluding bool's integer subtype."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError):
        return False


@dataclass(frozen=True)
class SignalObservation:
    """One numeric observation and its source provenance.

    ``observed_at`` must be the timestamp associated with the source value,
    not an invented timestamp used to make old data appear fresh. ``source``
    should be a stable provider or calculation identifier, and ``provenance``
    should state how the value was derived (without secrets or raw payloads).
    """

    name: str
    value: int | float | bool
    observed_at: datetime
    source: str
    provenance: str


@dataclass(frozen=True)
class SignalRule:
    """Scoring rule for one required signal.

    ``minimum`` and ``maximum`` define the accepted data domain and the
    normalization scale. Values outside the domain are rejected. A hard-block
    condition is useful for configured fraud/manipulation indicators, such as
    an active authority or observed liquidity withdrawal; the indicator itself
    must still be supplied by an evidenced upstream source.
    """

    name: str
    weight: float
    minimum: float
    maximum: float
    max_age: timedelta
    higher_is_better: bool = True
    allow_boolean: bool = False
    block_above: float | None = None
    block_below: float | None = None
    block_reason_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("signal_rule_name_required")
        if not _finite_number(self.weight) or self.weight < 0:
            raise ValueError("signal_rule_weight_invalid")
        if (
            not _finite_number(self.minimum)
            or not _finite_number(self.maximum)
            or self.maximum <= self.minimum
            or not math.isfinite(self.maximum - self.minimum)
        ):
            raise ValueError("signal_rule_range_invalid")
        if not isinstance(self.max_age, timedelta) or self.max_age <= timedelta(0):
            raise ValueError("signal_rule_max_age_invalid")
        if not isinstance(self.higher_is_better, bool):
            raise ValueError("signal_rule_direction_invalid")
        if not isinstance(self.allow_boolean, bool):
            raise ValueError("signal_rule_boolean_policy_invalid")
        for condition in (self.block_above, self.block_below):
            if condition is not None and (
                not _finite_number(condition)
                or condition < self.minimum
                or condition > self.maximum
            ):
                raise ValueError("signal_rule_block_boundary_invalid")
        if self.block_above is not None and self.block_below is not None:
            raise ValueError("signal_rule_block_conditions_ambiguous")
        has_block = self.block_above is not None or self.block_below is not None
        has_reason = isinstance(self.block_reason_code, str) and bool(self.block_reason_code.strip())
        if has_block != has_reason:
            raise ValueError("signal_rule_block_reason_required")


@dataclass(frozen=True)
class ScoringProfile:
    """Versioned heuristic scoring policy supplied by the caller."""

    profile_id: str
    rules: tuple[SignalRule, ...]
    minimum_score: float

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id.strip():
            raise ValueError("scoring_profile_id_required")
        if not isinstance(self.rules, tuple) or not self.rules:
            raise ValueError("scoring_profile_rules_required")
        if any(not isinstance(rule, SignalRule) for rule in self.rules):
            raise ValueError("scoring_profile_rule_invalid")
        names = [rule.name for rule in self.rules]
        if len(names) != len(set(names)):
            raise ValueError("scoring_profile_duplicate_rule")
        if not any(rule.weight > 0 for rule in self.rules):
            raise ValueError("scoring_profile_positive_weight_required")
        if not math.isfinite(sum(rule.weight for rule in self.rules)):
            raise ValueError("scoring_profile_total_weight_invalid")
        if (
            not _finite_number(self.minimum_score)
            or self.minimum_score < 0
            or self.minimum_score > 100
        ):
            raise ValueError("scoring_profile_threshold_invalid")


@dataclass(frozen=True)
class SignalContribution:
    """Auditable normalized contribution from one accepted signal."""

    name: str
    raw_value: float
    normalized_value: float
    weighted_points: float
    source: str
    provenance: str
    observed_at: datetime


@dataclass(frozen=True)
class ScoreDecision:
    """Scoring outcome; invalid evidence never produces an eligible score."""

    profile_id: str
    status: str
    eligible: bool
    score: float | None
    reason_codes: tuple[str, ...]
    contributions: tuple[SignalContribution, ...]
    scored_at: datetime


def score_token_evidence(
    observations: Sequence[SignalObservation],
    *,
    profile: ScoringProfile,
    as_of: datetime,
) -> ScoreDecision:
    """Score token evidence using a supplied, explicit heuristic profile.

    Required signals missing or invalid, stale/future/naive timestamps,
    unknown signal names, and conflicting duplicates all reject the candidate
    without returning a score. Hard-block rules reject even when the numeric
    score meets the profile threshold. This function is pure and offline.
    """
    if not isinstance(profile, ScoringProfile):
        raise ValueError("invalid_scoring_profile")
    if not isinstance(as_of, datetime) or not _is_aware(as_of):
        raise ValueError("scoring_as_of_must_be_timezone_aware")
    scored_at = as_of.astimezone(timezone.utc)
    if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes)):
        return _rejected(profile, scored_at, ("invalid_observation_collection",))

    rules_by_name = {rule.name: rule for rule in profile.rules}
    observations_by_name: dict[str, SignalObservation] = {}
    reason_codes: list[str] = []

    for observation in observations:
        if not isinstance(observation, SignalObservation):
            reason_codes.append("invalid_observation")
            continue
        name = observation.name
        if not isinstance(name, str) or not name.strip():
            reason_codes.append("invalid_signal_name")
            continue
        if name not in rules_by_name:
            reason_codes.append("unknown_signal")
            continue
        previous = observations_by_name.get(name)
        if previous is not None:
            if previous != observation:
                reason_codes.append("conflicting_duplicate_signal")
            continue
        observations_by_name[name] = observation

        rule = rules_by_name[name]
        if not isinstance(observation.source, str) or not observation.source.strip():
            reason_codes.append("missing_signal_source")
        if not isinstance(observation.provenance, str) or not observation.provenance.strip():
            reason_codes.append("missing_signal_provenance")
        if not isinstance(observation.observed_at, datetime) or not _is_aware(
            observation.observed_at
        ):
            reason_codes.append("invalid_signal_timestamp")
            continue
        observed_at = observation.observed_at.astimezone(timezone.utc)
        if observed_at > scored_at:
            reason_codes.append("future_signal_timestamp")
        elif scored_at - observed_at > rule.max_age:
            reason_codes.append("stale_signal")

        if isinstance(observation.value, bool):
            if not rule.allow_boolean:
                reason_codes.append("boolean_signal_not_allowed")
                continue
            numeric_value = float(observation.value)
        elif _finite_number(observation.value):
            numeric_value = float(observation.value)
        else:
            reason_codes.append("invalid_signal_value")
            continue
        if not math.isfinite(numeric_value):
            reason_codes.append("invalid_signal_value")
        elif numeric_value < rule.minimum or numeric_value > rule.maximum:
            reason_codes.append("signal_value_out_of_range")

    for rule in profile.rules:
        if rule.name not in observations_by_name:
            reason_codes.append("missing_required_signal")

    if reason_codes:
        return _rejected(profile, scored_at, tuple(dict.fromkeys(reason_codes)))

    total_weight = sum(rule.weight for rule in profile.rules)
    contributions: list[SignalContribution] = []
    block_reasons: list[str] = []
    score = 0.0
    for rule in profile.rules:
        observation = observations_by_name[rule.name]
        value = float(observation.value)
        normalized = (value - rule.minimum) / (rule.maximum - rule.minimum)
        if not rule.higher_is_better:
            normalized = 1.0 - normalized
        weighted_points = normalized * rule.weight / total_weight * 100.0
        score += weighted_points
        contributions.append(
            SignalContribution(
                name=rule.name,
                raw_value=value,
                normalized_value=normalized,
                weighted_points=weighted_points,
                source=observation.source,
                provenance=observation.provenance,
                observed_at=observation.observed_at.astimezone(timezone.utc),
            )
        )
        if rule.block_above is not None and value > rule.block_above:
            block_reasons.append(rule.block_reason_code or "configured_hard_block")
        if rule.block_below is not None and value < rule.block_below:
            block_reasons.append(rule.block_reason_code or "configured_hard_block")

    reason_codes = list(dict.fromkeys(block_reasons))
    if score < profile.minimum_score:
        reason_codes.append("score_below_profile_threshold")
    eligible = not reason_codes
    return ScoreDecision(
        profile_id=profile.profile_id,
        status="eligible" if eligible else "rejected",
        eligible=eligible,
        score=round(score, 6),
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        contributions=tuple(contributions),
        scored_at=scored_at,
    )


def _rejected(
    profile: ScoringProfile,
    scored_at: datetime,
    reason_codes: tuple[str, ...],
) -> ScoreDecision:
    """Return a refusal that carries no numeric score."""
    return ScoreDecision(
        profile_id=profile.profile_id,
        status="rejected",
        eligible=False,
        score=None,
        reason_codes=reason_codes,
        contributions=(),
        scored_at=scored_at,
    )
