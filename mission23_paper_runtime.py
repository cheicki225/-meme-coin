"""Fail-closed composition of durable Helius signals and PAPER admission.

The real Helius callback currently lacks a provider-authenticated finality and
completeness proof and is therefore blocked here. The explicit SIMULATED path
exists for deterministic offline validation only; its position is never marked
eligible for validated accounting.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import logging
from typing import Any

import config
from helius_event_store import EventRecord
from mission13_admission import (
    PaperCandidateRequest,
    PaperSignalLedger,
    evaluate_paper_candidate,
)
from mission18_paper_admission import SQLitePaperAdmissionStore

log = logging.getLogger("mission23.paper_runtime")


@dataclass(frozen=True)
class PaperRuntimeResult:
    """Outcome of one PAPER candidate; refusal codes contain no source IDs."""

    status: str
    reason: str
    score: float | None = None
    position_opened: bool = False
    provenance_state: str = "UNKNOWN"


class Mission23PaperRuntime:
    """Coordinate policy, durable reservation, PAPER simulation and commit."""

    def __init__(self, admission_store: SQLitePaperAdmissionStore, trader: Any) -> None:
        self._store = admission_store
        self._trader = trader

    async def process_event(
        self,
        event: EventRecord | None,
        request: PaperCandidateRequest | None,
        *,
        source_wallet: str | None,
        token_mint: str | None,
        reason: str,
        proposed_exposure_usd: float,
    ) -> PaperRuntimeResult:
        """Apply Mission 13 and M18 transaction gates before a PAPER position.

        Helius and bot-state SQLite databases are separate. A crash between
        their commits leaves an event/reservation unresolved and is not retried
        into a new position automatically.
        """
        if str(getattr(config, "TRADING_MODE", "")).upper() != "PAPER":
            return PaperRuntimeResult("blocked", "paper_mode_required")
        if type(getattr(config, "LIVE_EXECUTION_ENABLED", False)) is not bool:
            return PaperRuntimeResult("blocked", "invalid_live_execution_setting")
        if getattr(config, "LIVE_EXECUTION_ENABLED", False) is True:
            return PaperRuntimeResult("blocked", "live_execution_must_remain_disabled")
        if type(getattr(self._trader, "require_mission23_admission", False)) is not bool:
            return PaperRuntimeResult("blocked", "invalid_paper_trader_gate")
        if event is None or not isinstance(event, EventRecord):
            return PaperRuntimeResult("blocked", "durable_event_required")
        if (
            type(event.event_id) is not int
            or event.event_id <= 0
            or not isinstance(event.correlation_id, str)
            or not event.correlation_id.strip()
            or not isinstance(event.stream, str)
            or not event.stream.strip()
            or type(event.sequence) is not int
            or event.sequence <= 0
        ):
            return PaperRuntimeResult("blocked", "durable_event_identity_invalid")
        if (
            type(event.slot) is not int
            or event.slot < 0
            or type(event.transaction_index) is not int
            or event.transaction_index < 0
        ):
            return PaperRuntimeResult("blocked", "canonical_event_order_required")

        payload = event.payload if isinstance(event.payload, dict) else {}
        synthetic = (
            event.stream.startswith("simulated:")
            and payload.get("provenance_state") == "SIMULATED"
            and payload.get("finality_state") == "SIMULATED"
            and payload.get("completeness_state") == "SIMULATED"
        )
        if not synthetic:
            # No current production callsite authenticates provider finality,
            # full-history completeness, and signal provenance as one proof.
            return PaperRuntimeResult("unknown", "live_signal_provenance_unverified")
        if not isinstance(source_wallet, str) or not source_wallet.strip():
            return PaperRuntimeResult("blocked", "source_wallet_required", provenance_state="SIMULATED")
        if not isinstance(token_mint, str) or not token_mint.strip():
            return PaperRuntimeResult("blocked", "token_mint_required", provenance_state="SIMULATED")
        data_store = getattr(self._trader, "data_store", None)
        if data_store is None:
            return PaperRuntimeResult("blocked", "paper_wallet_state_unavailable", provenance_state="SIMULATED")
        try:
            settings = data_store.get_wallet_settings(source_wallet.strip())
            buy_once = settings.get("buy_only_once", True) if isinstance(settings, dict) else True
            state = data_store.state
            if buy_once:
                positions = [
                    *(state.get("open_positions") or []),
                    *(state.get("closed_positions") or []),
                ]
                if any(
                    isinstance(item, dict)
                    and item.get("source_wallet") == source_wallet.strip()
                    and item.get("token_mint") == token_mint.strip()
                    for item in positions
                ):
                    return PaperRuntimeResult(
                        "blocked", "buy_once_already_recorded", provenance_state="SIMULATED"
                    )
        except Exception:
            return PaperRuntimeResult(
                "blocked", "paper_wallet_state_unavailable", provenance_state="SIMULATED"
            )
        if request is None or not isinstance(request, PaperCandidateRequest):
            return PaperRuntimeResult("blocked", "mission13_evidence_required", provenance_state="SIMULATED")

        signal_id = f"helius-event:{event.event_id}"
        correlation_key = f"helius-correlation:{event.correlation_id}"
        candidate = replace(
            request,
            signal_id=signal_id,
            correlation_key=correlation_key,
        )
        try:
            snapshot = self._store.snapshot()
        except Exception:
            log.error("PAPER admission blocked because durable risk state is unavailable")
            return PaperRuntimeResult("blocked", "paper_state_unavailable", provenance_state="SIMULATED")

        try:
            decision = evaluate_paper_candidate(
                candidate,
                mode="PAPER",
                as_of=datetime.now(timezone.utc),
                position_state=snapshot.state,
                proposed_exposure_usd=proposed_exposure_usd,
                configured_max_exposure_usd=config.MAX_OPEN_EXPOSURE_USD,
                configured_max_open_positions=config.MAX_OPEN_POSITIONS,
                configured_max_session_loss_usd=config.CIRCUIT_BREAKER_USD,
                signal_ledger=PaperSignalLedger(),
                reserved_exposure_usd=snapshot.reserved_exposure_usd,
                reserved_positions=snapshot.reserved_positions,
            )
        except Exception:
            log.error("PAPER admission blocked because Mission 13 evaluation failed")
            return PaperRuntimeResult("blocked", "mission13_evaluation_failed", provenance_state="SIMULATED")
        if not decision.eligible:
            return PaperRuntimeResult(
                "blocked",
                decision.reason_codes[0] if decision.reason_codes else "mission13_rejected",
                score=decision.score,
                provenance_state="SIMULATED",
            )

        reserved, reserve_reason = self._store.reserve_entry(
            signal_id,
            correlation_key,
            proposed_exposure_usd,
            max_exposure_usd=config.MAX_OPEN_EXPOSURE_USD,
            max_open_positions=config.MAX_OPEN_POSITIONS,
            max_session_loss_usd=config.CIRCUIT_BREAKER_USD,
        )
        if not reserved:
            if reserve_reason in {
                "duplicate_signal",
                "correlated_signal",
                "reservation_identity_conflict",
            }:
                # A different worker may still be resolving this same durable
                # intent. Do not let its duplicate callback ACK the Helius event
                # while the first worker is pending or after an ambiguous crash.
                # Leave the existing reservation untouched for its owner.
                return PaperRuntimeResult(
                    "unknown", "durable_reservation_outcome_unresolved",
                    score=decision.score, provenance_state="SIMULATED",
                )
            return PaperRuntimeResult(
                "blocked", reserve_reason or "durable_reservation_rejected",
                score=decision.score, provenance_state="SIMULATED",
            )

        try:
            position = await self._trader.open_position(
                token_mint.strip(),
                source_wallet.strip(),
                reason,
                creation_slot=event.slot,
                mission18_admission_store=self._store,
                mission18_signal_id=signal_id,
                provenance_state="SIMULATED",
            )
        except Exception:
            self._store.mark_unknown(signal_id)
            log.error("PAPER admission remains reserved UNKNOWN after simulation failure")
            return PaperRuntimeResult(
                "unknown", "paper_simulation_or_commit_failed",
                score=decision.score, provenance_state="SIMULATED",
            )
        if position is None:
            self._store.mark_unknown(signal_id)
            return PaperRuntimeResult(
                "unknown", "paper_simulation_did_not_commit",
                score=decision.score, provenance_state="SIMULATED",
            )
        return PaperRuntimeResult(
            "committed", "paper_position_committed", score=decision.score,
            position_opened=True, provenance_state="SIMULATED",
        )
