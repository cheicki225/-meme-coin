"""Runtime safety helpers for trading execution.

This module deliberately has no network or wallet dependencies. Callers should
invoke ``preflight_buy`` immediately before creating a buy transaction and
persist the returned idempotency key with the order intent.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Mapping, MutableMapping

from safety_guard import OrderLimits, SafetySnapshot, validate_order


@dataclass(frozen=True)
class OrderIntent:
    side: str
    token_mint: str
    amount_sol: float
    source_wallet: str
    created_at: float
    idempotency_key: str


def make_idempotency_key(
    *, side: str, token_mint: str, source_wallet: str, event_id: str | None = None
) -> str:
    """Create a deterministic key for one signal/order intent."""
    raw = "|".join(
        (
            side.strip().lower(),
            token_mint.strip(),
            source_wallet.strip(),
            event_id or "",
        )
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"{side.strip().lower()}:{digest}"


def snapshot_from_state(state: Mapping[str, Any]) -> SafetySnapshot:
    """Build a conservative snapshot from the bot's persisted state.

    Missing or malformed values are treated as unsafe by ``validate_order``.
    """
    return SafetySnapshot(
        balance_sol=float(state.get("balance_sol", 0.0)),
        daily_pnl_sol=float(state.get("daily_pnl_sol", state.get("session_pnl_sol", 0.0))),
        open_positions=int(len(state.get("open_positions", []) or [])),
        already_exposed_sol=float(state.get("already_exposed_sol", 0.0)),
    )


def preflight_buy(
    *,
    token_mint: str,
    amount_sol: float,
    source_wallet: str,
    settings: Mapping[str, Any],
    state: Mapping[str, Any],
    event_id: str | None = None,
    emergency_stop: bool = False,
) -> OrderIntent:
    """Fail closed before a buy is allowed to proceed."""
    key = make_idempotency_key(
        side="buy",
        token_mint=token_mint,
        source_wallet=source_wallet,
        event_id=event_id,
    )
    limits = OrderLimits(
        max_position_sol=float(settings.get("max_position_sol", 0.5)),
        max_daily_loss_sol=float(settings.get("max_daily_loss_sol", 1.0)),
        max_open_positions=int(settings.get("max_open_positions", 3)),
        min_balance_reserve_sol=float(settings.get("min_balance_reserve_sol", 0.05)),
    )
    validate_order(
        amount_sol,
        limits,
        snapshot_from_state(state),
        emergency_stop=emergency_stop,
        idempotency_key=key,
    )
    return OrderIntent(
        side="buy",
        token_mint=token_mint,
        amount_sol=amount_sol,
        source_wallet=source_wallet,
        created_at=time.time(),
        idempotency_key=key,
    )


def reserve_intent(state: MutableMapping[str, Any], intent: OrderIntent) -> None:
    """Persist a pending order intent before any network submission.

    A pending/confirmed/unknown intent is never submitted twice. Failed intents
    may be retried intentionally because the executor reported a definite failure.
    """
    registry = state.setdefault("safety_order_intents", {})
    if not isinstance(registry, dict):
        raise ValueError("Invalid safety order registry")

    existing = registry.get(intent.idempotency_key)
    if isinstance(existing, dict) and existing.get("status") in {"pending", "confirmed", "unknown"}:
        raise ValueError("Duplicate or unresolved order intent")

    registry[intent.idempotency_key] = {
        "side": intent.side,
        "token_mint": intent.token_mint,
        "amount_sol": intent.amount_sol,
        "source_wallet": intent.source_wallet,
        "created_at": intent.created_at,
        "status": "pending",
    }


def mark_intent(
    state: MutableMapping[str, Any],
    idempotency_key: str,
    status: str,
    *,
    signature: str | None = None,
    error: str | None = None,
) -> None:
    """Update a persisted order intent after submission/confirmation."""
    if status not in {"pending", "confirmed", "failed", "unknown"}:
        raise ValueError("Invalid order intent status")

    registry = state.get("safety_order_intents")
    if not isinstance(registry, dict):
        raise ValueError("Missing safety order registry")
    record = registry.get(idempotency_key)
    if not isinstance(record, dict):
        raise ValueError("Unknown order intent")

    record["status"] = status
    record["updated_at"] = time.time()
    if signature:
        record["signature"] = signature
    if error:
        record["error"] = error[:500]
