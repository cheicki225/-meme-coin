"""PAPER-only composition for Mission 13 scoring, regime, strategy and risk policies.

This adapter consumes caller-supplied evidence only. It does not fetch data,
create signals, execute trades, or claim safety or profitability.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from threading import Lock
from typing import Sequence

import risk_controls
from mission13_market_policy import (
    RegimeAssessment, RegimeObservation, RegimePolicy, StrategyCandidate,
    StrategySelection, classify_market_regime, select_paper_strategies,
)
from mission13_scoring import (
    ScoringProfile, ScoreDecision, SignalContribution, SignalObservation,
    score_token_evidence,
)


def _aware_utc(value: object) -> datetime | None:
    """Normalize an aware datetime to UTC."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _finite_nonnegative(value: object) -> bool:
    """Validate a finite nonnegative number, excluding bool."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except (OverflowError, TypeError):
        return False


def _valid_key(value: object) -> bool:
    """Validate an opaque identifier without returning it in errors."""
    return isinstance(value, str) and bool(value.strip()) and len(value.strip()) <= 200


class PaperSignalLedger:
    """In-process duplicate/correlation guard; durable storage remains caller-owned.

    Seed committed pairs from durable storage when constructing the ledger.
    This class does not claim restart-safe idempotency.
    """

    def __init__(self, committed: Sequence[tuple[str, str]] = ()) -> None:
        self._lock = Lock()
        self._committed: dict[str, str] = {}
        self._pending: dict[str, str] = {}
        if not isinstance(committed, Sequence) or isinstance(committed, (str, bytes)):
            raise ValueError("invalid_committed_signal_records")
        for item in committed:
            if (
                not isinstance(item, tuple) or len(item) != 2
                or not _valid_key(item[0]) or not _valid_key(item[1])
            ):
                raise ValueError("invalid_committed_signal_record")
            signal_id, group = item[0].strip(), item[1].strip()
            if signal_id in self._committed or group in self._committed.values():
                raise ValueError("duplicate_or_correlated_committed_signal")
            self._committed[signal_id] = group

    def reserve(self, signal_id: str, correlation_key: str) -> tuple[bool, str | None]:
        """Atomically reserve one signal identifier and correlation group."""
        if not _valid_key(signal_id) or not _valid_key(correlation_key):
            return False, "invalid_signal_identity"
        signal_id, correlation_key = signal_id.strip(), correlation_key.strip()
        with self._lock:
            if signal_id in self._committed or signal_id in self._pending:
                return False, "duplicate_signal"
            if correlation_key in set(self._committed.values()) | set(self._pending.values()):
                return False, "correlated_signal"
            self._pending[signal_id] = correlation_key
            return True, None

    def commit(self, signal_id: str) -> bool:
        """Promote a pending signal after a PAPER decision is durably recorded."""
        if not _valid_key(signal_id):
            return False
        signal_id = signal_id.strip()
        with self._lock:
            group = self._pending.pop(signal_id, None)
            if group is None:
                return False
            self._committed[signal_id] = group
            return True

    def release(self, signal_id: str) -> bool:
        """Release a pending signal when the PAPER simulation did not open."""
        if not _valid_key(signal_id):
            return False
        with self._lock:
            return self._pending.pop(signal_id.strip(), None) is not None

    def snapshot(self) -> tuple[tuple[str, str, str], ...]:
        """Return immutable, state-labelled identities for recovery."""
        with self._lock:
            return tuple(sorted([("committed", signal_id, group) for signal_id, group in self._committed.items()] + [("pending", signal_id, group) for signal_id, group in self._pending.items()]))


@dataclass(frozen=True)
class PaperCandidateRequest:
    """Evidence and policy registrations for a candidate at one decision time."""

    signal_id: str
    correlation_key: str
    observations: tuple[SignalObservation, ...]
    scoring_profile: ScoringProfile
    regime_observations: tuple[RegimeObservation, ...]
    regime_policy: RegimePolicy
    strategies: tuple[StrategyCandidate, ...]


@dataclass(frozen=True)
class PaperAdmissionDecision:
    """Auditable outcome; eligible means admitted to the PAPER path only."""

    status: str
    reason_codes: tuple[str, ...]
    signal_id: str
    correlation_key: str
    score: float | None
    profile_id: str | None
    regime: str
    strategy_id: str | None
    assessed_at: datetime
    contributions: tuple[SignalContribution, ...] = ()
    reservation_id: str | None = None

    @property
    def eligible(self) -> bool:
        """Whether the candidate passed all supplied PAPER admission checks."""
        return self.status == "eligible"


def _rejected(
    reasons: Sequence[str],
    *,
    signal_id: str = "",
    correlation_key: str = "",
    assessed_at: datetime | None = None,
    score: ScoreDecision | None = None,
    regime: RegimeAssessment | None = None,
    strategy: StrategySelection | None = None,
) -> PaperAdmissionDecision:
    """Build a refusal retaining policy details needed for an audit."""
    regime_name = getattr(getattr(regime, "regime", None), "value", "unknown")
    return PaperAdmissionDecision(
        status="rejected",
        reason_codes=tuple(dict.fromkeys(reasons)) or ("candidate_rejected",),
        signal_id=signal_id if _valid_key(signal_id) else "",
        correlation_key=correlation_key if _valid_key(correlation_key) else "",
        score=score.score if score is not None else None,
        profile_id=score.profile_id if score is not None else None,
        regime=regime_name,
        strategy_id=strategy.strategy_id if strategy is not None else None,
        assessed_at=assessed_at or datetime.now(timezone.utc),
        contributions=score.contributions if score is not None else (),
    )


def evaluate_paper_candidate(
    request: PaperCandidateRequest,
    *,
    mode: str,
    as_of: datetime,
    position_state: dict,
    proposed_exposure_usd: float,
    configured_max_exposure_usd: float,
    configured_max_open_positions: int,
    configured_max_session_loss_usd: float,
    signal_ledger: PaperSignalLedger,
    reserved_exposure_usd: float | None = None,
    reserved_positions: int | None = None,
) -> PaperAdmissionDecision:
    """Evaluate and reserve one candidate for PAPER simulation only.

    Profiles and thresholds are supplied by the caller. Configured risk limits
    must be copied unchanged from the bot configuration. Existing exposure and
    position limits are delegated to risk_controls. Missing reservation snapshots
    fail closed. The caller commits the
    returned reservation after persisting an opened PAPER position, or releases
    it if the following simulation does not create one.
    """
    decision_time = _aware_utc(as_of)
    if decision_time is None:
        return _rejected(("invalid_as_of",))
    normalized_mode = mode.strip().upper() if isinstance(mode, str) else ""
    if normalized_mode != "PAPER":
        return _rejected(("paper_mode_required",), assessed_at=decision_time)
    if not isinstance(request, PaperCandidateRequest):
        return _rejected(("invalid_candidate_request",), assessed_at=decision_time)

    signal_id = request.signal_id.strip() if _valid_key(request.signal_id) else ""
    correlation_key = request.correlation_key.strip() if _valid_key(request.correlation_key) else ""
    if not signal_id or not correlation_key:
        return _rejected(("invalid_signal_identity",), assessed_at=decision_time)
    if not isinstance(signal_ledger, PaperSignalLedger):
        return _rejected(
            ("signal_ledger_required",), signal_id=signal_id,
            correlation_key=correlation_key, assessed_at=decision_time,
        )
    if (
        not isinstance(request.observations, tuple)
        or not isinstance(request.regime_observations, tuple)
        or not isinstance(request.strategies, tuple)
    ):
        return _rejected(
            ("candidate_inputs_must_be_immutable_sequences",), signal_id=signal_id,
            correlation_key=correlation_key, assessed_at=decision_time,
        )

    try:
        score = score_token_evidence(
            request.observations, profile=request.scoring_profile, as_of=decision_time,
        )
    except (TypeError, ValueError, ArithmeticError):
        return _rejected(
            ("invalid_scoring_configuration",), signal_id=signal_id,
            correlation_key=correlation_key, assessed_at=decision_time,
        )
    if score.status != "eligible" or not score.eligible or score.score is None:
        return _rejected(
            score.reason_codes or ("score_rejected",), signal_id=signal_id,
            correlation_key=correlation_key, assessed_at=decision_time, score=score,
        )

    try:
        regime = classify_market_regime(
            request.regime_observations, as_of=decision_time, policy=request.regime_policy,
        )
    except (TypeError, ValueError, ArithmeticError):
        return _rejected(
            ("invalid_regime_configuration",), signal_id=signal_id,
            correlation_key=correlation_key, assessed_at=decision_time, score=score,
        )
    if not regime.ready:
        return _rejected(
            (regime.reason_code or "market_regime_unavailable",), signal_id=signal_id,
            correlation_key=correlation_key, assessed_at=decision_time,
            score=score, regime=regime,
        )

    available = frozenset(
        item.name for item in request.observations
        if isinstance(item, SignalObservation) and isinstance(item.name, str)
    )
    strategy = select_paper_strategies(
        regime, request.strategies, available_signals=available, mode=normalized_mode,
    )
    if strategy.status != "eligible" or strategy.strategy_id is None:
        return _rejected(
            strategy.reason_codes or ("strategy_rejected",), signal_id=signal_id,
            correlation_key=correlation_key, assessed_at=decision_time,
            score=score, regime=regime, strategy=strategy,
        )

    if not isinstance(position_state, dict):
        return _rejected(
            ("invalid_position_state",), signal_id=signal_id, correlation_key=correlation_key,
            assessed_at=decision_time, score=score, regime=regime, strategy=strategy,
        )
    emergency_stop = position_state.get("emergency_stop")
    if not isinstance(emergency_stop, bool):
        return _rejected(
            ("invalid_emergency_stop_state",), signal_id=signal_id, correlation_key=correlation_key,
            assessed_at=decision_time, score=score, regime=regime, strategy=strategy,
        )
    if emergency_stop:
        return _rejected(
            ("emergency_stop_active",), signal_id=signal_id, correlation_key=correlation_key,
            assessed_at=decision_time, score=score, regime=regime, strategy=strategy,
        )
    losses = position_state.get("session_losses_usd")
    if (
        not _finite_nonnegative(losses) or not _finite_nonnegative(configured_max_session_loss_usd)
        or configured_max_session_loss_usd <= 0
    ):
        return _rejected(
            ("invalid_session_loss_state_or_limit",), signal_id=signal_id,
            correlation_key=correlation_key, assessed_at=decision_time,
            score=score, regime=regime, strategy=strategy,
        )
    if float(losses) >= float(configured_max_session_loss_usd):
        return _rejected(
            ("session_loss_limit_reached",), signal_id=signal_id,
            correlation_key=correlation_key, assessed_at=decision_time,
            score=score, regime=regime, strategy=strategy,
        )

    if (
        not _finite_nonnegative(reserved_exposure_usd)
        or type(reserved_positions) is not int
        or reserved_positions < 0
    ):
        return _rejected(
            ("invalid_reserved_risk_snapshot",), signal_id=signal_id,
            correlation_key=correlation_key, assessed_at=decision_time,
            score=score, regime=regime, strategy=strategy,
        )
    allowed, reason = risk_controls.assess_new_position(
        position_state, proposed_exposure_usd,
        max_exposure_usd=configured_max_exposure_usd,
        max_open_positions=configured_max_open_positions,
        reserved_exposure_usd=reserved_exposure_usd,
        reserved_positions=reserved_positions,
    )
    if not allowed:
        return _rejected(
            (reason or "risk_control_rejected",), signal_id=signal_id,
            correlation_key=correlation_key, assessed_at=decision_time,
            score=score, regime=regime, strategy=strategy,
        )

    reserved, reason = signal_ledger.reserve(signal_id, correlation_key)
    if not reserved:
        return _rejected(
            (reason or "signal_reservation_rejected",), signal_id=signal_id,
            correlation_key=correlation_key, assessed_at=decision_time,
            score=score, regime=regime, strategy=strategy,
        )

    return PaperAdmissionDecision(
        status="eligible",
        reason_codes=(
            "paper_mode", "score_eligible", "regime_classified",
            "strategy_selected", "risk_limits_passed",
        ),
        signal_id=signal_id,
        correlation_key=correlation_key,
        score=score.score,
        profile_id=score.profile_id,
        regime=regime.regime.value,
        strategy_id=strategy.strategy_id,
        assessed_at=decision_time,
        contributions=score.contributions,
        reservation_id=signal_id,
    )
