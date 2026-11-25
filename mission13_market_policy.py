"""Explainable market-regime and PAPER strategy selection policies.

The module consumes caller-supplied observations only. It performs no network
access, creates no trading signal, and does not execute or simulate a trade.
Thresholds are explicit heuristics; they are not calibrated or validated on
real Solana market data.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import math
from statistics import pstdev
from typing import Iterable, Sequence


class MarketRegime(str, Enum):
    """Small set of descriptive regimes for a supplied price series."""

    BULLISH_TREND = "bullish_trend"
    BEARISH_TREND = "bearish_trend"
    SIDEWAYS = "sideways"
    HIGH_VOLATILITY = "high_volatility"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RegimeObservation:
    """A point-in-time price observation with a non-secret source identifier.

    ``observed_at`` is when the caller says this value was observed. It does
    not prove the provider's underlying data timestamp or authenticity.
    """

    observed_at: datetime
    close: float
    source: str


@dataclass(frozen=True)
class RegimePolicy:
    """Caller-configurable thresholds and exact cadence for regime classification."""

    min_observations: int
    expected_interval: timedelta
    max_latest_age: timedelta
    max_observation_gap: timedelta
    trend_threshold_pct: float
    high_volatility_threshold_pct: float


@dataclass(frozen=True)
class RegimeAssessment:
    """Auditable output from the deterministic regime classifier."""

    regime: MarketRegime
    reason_code: str
    as_of: datetime | None
    observation_count: int
    sources: tuple[str, ...]
    trend_pct: float | None = None
    volatility_pct: float | None = None

    @property
    def ready(self) -> bool:
        """Whether the supplied series was sufficient for classification."""

        return (
            isinstance(self.regime, MarketRegime)
            and self.regime is not MarketRegime.UNKNOWN
            and _utc(self.as_of) is not None
            and type(self.observation_count) is int
            and self.observation_count >= 2
            and isinstance(self.sources, tuple)
            and bool(self.sources)
            and all(isinstance(source, str) and source.strip() for source in self.sources)
            and _finite_number(self.trend_pct)
            and _finite_number(self.volatility_pct)
            and self.volatility_pct >= 0
        )


@dataclass(frozen=True)
class StrategyCandidate:
    """A registered strategy or an existing bot mode eligible for evaluation.

    ``kind`` may be ``rule``, ``sniping_existing``, or
    ``copy_trading_existing``. The latter two are identifiers for existing
    signal sources only; this module does not implement their behavior.
    """

    strategy_id: str
    kind: str
    compatible_regimes: frozenset[MarketRegime]
    required_signals: frozenset[str]
    enabled: bool = True
    priority: int = 0


@dataclass(frozen=True)
class StrategySelection:
    """PAPER-only selection result; it is not an order or trade decision."""

    status: str
    strategy_id: str | None
    regime: MarketRegime
    reason_codes: tuple[str, ...]
    rejected: tuple[tuple[str, str], ...]
    mode: str

    @property
    def accepted(self) -> bool:
        """Compatibility convenience for callers that need a boolean."""

        return self.status == "eligible"

    @property
    def selected_strategy_ids(self) -> tuple[str, ...]:
        """Expose zero or one selected strategy as an immutable tuple."""

        return (self.strategy_id,) if self.strategy_id is not None else ()

    @property
    def reason_code(self) -> str:
        """Expose the first reason as a single-code convenience."""

        return self.reason_codes[0] if self.reason_codes else "unspecified"


def _utc(value: datetime) -> datetime | None:
    """Normalize an aware datetime; return ``None`` for naive values."""

    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _finite_number(value: object) -> bool:
    """Check supported numeric inputs without bools or integer overflow."""

    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _valid_policy(policy: RegimePolicy) -> bool:
    if not isinstance(policy, RegimePolicy):
        return False
    return (
        type(policy.min_observations) is int
        and policy.min_observations >= 2
        and isinstance(policy.expected_interval, timedelta)
        and policy.expected_interval > timedelta(0)
        and isinstance(policy.max_latest_age, timedelta)
        and policy.max_latest_age > timedelta(0)
        and isinstance(policy.max_observation_gap, timedelta)
        and policy.max_observation_gap > timedelta(0)
        and policy.expected_interval <= policy.max_observation_gap
        and _finite_number(policy.trend_threshold_pct)
        and policy.trend_threshold_pct > 0
        and _finite_number(policy.high_volatility_threshold_pct)
        and policy.high_volatility_threshold_pct > 0
    )


def classify_market_regime(
    observations: Iterable[RegimeObservation],
    *,
    as_of: datetime,
    policy: RegimePolicy,
) -> RegimeAssessment:
    """Classify only the explicit, chronological observations up to ``as_of``.

    A stale, malformed, future-dated, out-of-order, or sparse series returns
    ``UNKNOWN``. Returns are close-to-close percentage changes; volatility is
    their population standard deviation. High volatility takes precedence over
    trend direction. The calculation is descriptive, not predictive.
    """

    normalized_as_of = _utc(as_of)
    if normalized_as_of is None:
        return RegimeAssessment(MarketRegime.UNKNOWN, "invalid_as_of", None, 0, ())
    if not _valid_policy(policy):
        return RegimeAssessment(MarketRegime.UNKNOWN, "invalid_policy", normalized_as_of, 0, ())

    try:
        rows = tuple(observations)
    except TypeError:
        rows = ()
    if len(rows) < policy.min_observations:
        return RegimeAssessment(
            MarketRegime.UNKNOWN, "insufficient_observations", normalized_as_of, len(rows), ()
        )

    timestamps: list[datetime] = []
    closes: list[float] = []
    sources: list[str] = []
    for row in rows:
        if not isinstance(row, RegimeObservation):
            return RegimeAssessment(
                MarketRegime.UNKNOWN, "invalid_observation", normalized_as_of, len(rows), ()
            )
        timestamp = _utc(row.observed_at)
        if timestamp is None:
            return RegimeAssessment(
                MarketRegime.UNKNOWN, "invalid_observation_timestamp", normalized_as_of, len(rows), ()
            )
        if timestamp > normalized_as_of:
            return RegimeAssessment(
                MarketRegime.UNKNOWN, "future_observation", normalized_as_of, len(rows), ()
            )
        if timestamps and timestamp <= timestamps[-1]:
            return RegimeAssessment(
                MarketRegime.UNKNOWN, "observations_not_strictly_increasing", normalized_as_of, len(rows), ()
            )

        if not _finite_number(row.close) or row.close <= 0:
            return RegimeAssessment(
                MarketRegime.UNKNOWN, "invalid_close_price", normalized_as_of, len(rows), ()
            )
        if not isinstance(row.source, str) or not row.source.strip():
            return RegimeAssessment(
                MarketRegime.UNKNOWN, "missing_source_provenance", normalized_as_of, len(rows), ()
            )
        timestamps.append(timestamp)
        closes.append(float(row.close))
        sources.append(row.source.strip())

    intervals = [current - previous for previous, current in zip(timestamps, timestamps[1:])]
    if any(interval > policy.max_observation_gap for interval in intervals):
        return RegimeAssessment(
            MarketRegime.UNKNOWN, "observation_gap_exceeded", normalized_as_of, len(rows), tuple(sorted(set(sources)))
        )
    if any(interval != policy.expected_interval for interval in intervals):
        return RegimeAssessment(
            MarketRegime.UNKNOWN, "inconsistent_observation_interval", normalized_as_of, len(rows), tuple(sorted(set(sources)))
        )
    if normalized_as_of - timestamps[-1] > policy.max_latest_age:
        return RegimeAssessment(
            MarketRegime.UNKNOWN, "stale_latest_observation", normalized_as_of, len(rows), tuple(sorted(set(sources)))
        )

    returns_pct = [((current / previous) - 1.0) * 100.0 for previous, current in zip(closes, closes[1:])]
    if any(not math.isfinite(value) for value in returns_pct):
        return RegimeAssessment(
            MarketRegime.UNKNOWN, "invalid_price_return", normalized_as_of, len(rows), tuple(sorted(set(sources)))
        )
    trend_pct = ((closes[-1] / closes[0]) - 1.0) * 100.0
    volatility_pct = pstdev(returns_pct)
    if not math.isfinite(trend_pct) or not math.isfinite(volatility_pct):
        return RegimeAssessment(
            MarketRegime.UNKNOWN, "invalid_regime_metric", normalized_as_of, len(rows), tuple(sorted(set(sources)))
        )

    if volatility_pct >= policy.high_volatility_threshold_pct:
        regime = MarketRegime.HIGH_VOLATILITY
        reason = "high_volatility_threshold_reached"
    elif trend_pct >= policy.trend_threshold_pct:
        regime = MarketRegime.BULLISH_TREND
        reason = "positive_trend_threshold_reached"
    elif trend_pct <= -policy.trend_threshold_pct:
        regime = MarketRegime.BEARISH_TREND
        reason = "negative_trend_threshold_reached"
    else:
        regime = MarketRegime.SIDEWAYS
        reason = "trend_within_sideways_band"
    return RegimeAssessment(
        regime=regime,
        reason_code=reason,
        as_of=normalized_as_of,
        observation_count=len(rows),
        sources=tuple(sorted(set(sources))),
        trend_pct=trend_pct,
        volatility_pct=volatility_pct,
    )


def select_paper_strategies(
    assessment: RegimeAssessment,
    candidates: Sequence[StrategyCandidate],
    *,
    available_signals: Iterable[str],
    mode: str,
) -> StrategySelection:
    """Return compatible registered IDs only while explicitly in PAPER mode.

    Existing sniping/copy-trading entries can be represented as candidate
    identifiers, but their signal generation, execution, and risk controls stay
    in their existing components. No strategy is selected on unknown regime or
    when its required inputs are absent.
    """

    normalized_mode = mode.strip().upper() if isinstance(mode, str) else ""
    regime = assessment.regime if isinstance(assessment, RegimeAssessment) else MarketRegime.UNKNOWN
    if normalized_mode != "PAPER":
        return StrategySelection(
            "rejected", None, regime, ("paper_mode_required",), (), normalized_mode or "UNKNOWN"
        )
    if (
        not isinstance(assessment, RegimeAssessment)
        or not isinstance(assessment.regime, MarketRegime)
        or not assessment.ready
    ):
        return StrategySelection(
            "rejected", None, MarketRegime.UNKNOWN, ("market_regime_unavailable",), (), normalized_mode
        )
    if not isinstance(candidates, Sequence) or not candidates:
        return StrategySelection(
            "rejected", None, regime, ("no_registered_strategies",), (), normalized_mode
        )

    if isinstance(available_signals, str):
        return StrategySelection(
            "rejected", None, regime, ("invalid_available_signals",), (), normalized_mode
        )
    try:
        available = frozenset(available_signals)
    except TypeError:
        return StrategySelection(
            "rejected", None, regime, ("invalid_available_signals",), (), normalized_mode
        )
    if any(not isinstance(signal, str) or not signal.strip() for signal in available):
        return StrategySelection(
            "rejected", None, regime, ("invalid_available_signals",), (), normalized_mode
        )

    allowed_kinds = {"rule", "sniping_existing", "copy_trading_existing"}
    ids: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, StrategyCandidate):
            return StrategySelection(
                "rejected", None, regime, ("invalid_strategy_registration",), (), normalized_mode
            )
        if (
            not isinstance(candidate.strategy_id, str)
            or not candidate.strategy_id.strip()
            or not isinstance(candidate.kind, str)
            or candidate.kind not in allowed_kinds
            or not isinstance(candidate.compatible_regimes, frozenset)
            or not isinstance(candidate.required_signals, frozenset)
            or any(not isinstance(signal, str) or not signal.strip() for signal in candidate.required_signals)
            or any(not isinstance(candidate_regime, MarketRegime) for candidate_regime in candidate.compatible_regimes)
            or not isinstance(candidate.enabled, bool)
            or type(candidate.priority) is not int
        ):
            return StrategySelection(
                "rejected", None, regime, ("invalid_strategy_registration",), (), normalized_mode
            )
        ids.append(candidate.strategy_id)
    if len(set(ids)) != len(ids):
        return StrategySelection("rejected", None, regime, ("duplicate_strategy_id",), (), normalized_mode)

    rejected: list[tuple[str, str]] = []
    for candidate in candidates:
        if not candidate.enabled:
            rejected.append((candidate.strategy_id, "strategy_disabled"))
        elif assessment.regime not in candidate.compatible_regimes:
            rejected.append((candidate.strategy_id, "regime_incompatible"))
        elif not candidate.required_signals.issubset(available):
            rejected.append((candidate.strategy_id, "required_signal_missing"))

    eligible = [
        candidate
        for candidate in candidates
        if candidate.enabled
        and assessment.regime in candidate.compatible_regimes
        and candidate.required_signals.issubset(available)
    ]
    if not eligible:
        return StrategySelection(
            "rejected", None, regime, ("no_eligible_strategy",), tuple(rejected), normalized_mode
        )
    selected = min(eligible, key=lambda candidate: (-candidate.priority, candidate.strategy_id))
    return StrategySelection(
        "eligible",
        selected.strategy_id,
        regime,
        ("paper_mode", "regime_compatible", "required_signals_present"),
        tuple(rejected),
        normalized_mode,
    )
