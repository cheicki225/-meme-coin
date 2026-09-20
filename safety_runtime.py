"""Runtime safety helpers for trading execution.

This module deliberately has no network or wallet dependencies. Callers should
invoke ``preflight_buy`` immediately before creating a buy transaction and
persist the returned idempotency key with the order intent.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Mapping

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
