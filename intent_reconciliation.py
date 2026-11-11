"""Helpers for safely reviewing unresolved live order intents.

This module is intentionally network-free: reconciliation must be performed by
an operator or a provider-specific adapter that can verify chain state.
"""

from __future__ import annotations

from typing import Any, Mapping

UNRESOLVED_STATUSES = frozenset({"pending", "unknown"})


def list_unresolved_intents(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return unresolved intents with their idempotency key included."""
    registry = state.get("safety_order_intents", {})
    if not isinstance(registry, dict):
        return []

    unresolved: list[dict[str, Any]] = []
    for key, record in registry.items():
        if not isinstance(record, dict):
            continue
        if record.get("status") in UNRESOLVED_STATUSES:
            item = dict(record)
            item["idempotency_key"] = key
            unresolved.append(item)
    def _created_at(item):
        try:
            value = float(item.get("created_at", 0.0))
            return value if value >= 0 else 0.0
        except (TypeError, ValueError, OverflowError):
            return 0.0

    return sorted(unresolved, key=_created_at)


def resolve_intent(
    state: dict[str, Any],
    idempotency_key: str,
    *,
    status: str,
    signature: str | None = None,
    note: str | None = None,
) -> None:
    """Apply an operator-confirmed resolution; never silently retry an intent."""
    if status not in {"confirmed", "failed", "unknown"}:
        raise ValueError("Invalid reconciliation status")
    registry = state.get("safety_order_intents")
    if not isinstance(registry, dict) or not isinstance(registry.get(idempotency_key), dict):
        raise ValueError("Unknown order intent")

    record = registry[idempotency_key]
    if record.get("status") not in UNRESOLVED_STATUSES:
        raise ValueError("Only pending or unknown intents can be reconciled")
    if status == "unknown":
        raise ValueError("Reconciliation must establish confirmed or failed on-chain outcome")
    record["status"] = status
    if signature:
        record["signature"] = signature
    if note:
        record["reconciliation_note"] = note[:500]

def list_unresolved_sell_intents(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """List pending/unknown/confirmed-unaccounted sell intents without mutating state."""
    pending = state.get("safety_pending_sell_tokens", {})
    unresolved = state.get("safety_unresolved_sell_tokens", {})
    unaccounted = state.get("safety_unaccounted_confirmed_sells", {})
    if not all(isinstance(registry, dict) for registry in (pending, unresolved, unaccounted)):
        raise ValueError("Sell reconciliation registry is corrupted")

    items: list[dict[str, Any]] = []
    for token_mint, record in pending.items():
        if isinstance(record, dict):
            item = dict(record)
            item.update({"token_mint": token_mint, "registry": "pending"})
            items.append(item)
    for token_mint, record in unresolved.items():
        if isinstance(record, dict):
            item = dict(record)
            item.update({"token_mint": token_mint, "registry": "unresolved"})
            items.append(item)
    for token_mint, record in unaccounted.items():
        if isinstance(record, dict):
            item = dict(record)
            item.update({"token_mint": token_mint, "registry": "confirmed_unaccounted"})
            items.append(item)

    def _created_at(item):
        try:
            value = float(item.get("created_at", 0.0))
            return value if value >= 0 else 0.0
        except (TypeError, ValueError, OverflowError):
            return 0.0

    return sorted(items, key=_created_at)


def resolve_sell_intent(
    state: dict[str, Any],
    token_mint: str,
    *,
    status: str,
    signature: str | None = None,
    note: str | None = None,
) -> None:
    """Record an operator-verified chain outcome; confirmed sells stay blocked for accounting."""
    if status not in {"confirmed", "failed"}:
        raise ValueError("Sell reconciliation requires a verified confirmed or failed outcome")
    pending = state.get("safety_pending_sell_tokens")
    unresolved = state.get("safety_unresolved_sell_tokens")
    unaccounted = state.get("safety_unaccounted_confirmed_sells")
    if not all(isinstance(registry, dict) for registry in (pending, unresolved, unaccounted)):
        raise ValueError("Sell reconciliation registry is missing or corrupted")
    record = pending.get(token_mint)
    if not isinstance(record, dict) or record.get("status") not in {"submitting", "unknown"}:
        raise ValueError("Only submitting or unknown sell intents can be reconciled")

    if status == "failed":
        pending.pop(token_mint)
        unresolved.pop(token_mint, None)
        return

    if not isinstance(signature, str) or not signature.strip():
        raise ValueError("A verified transaction signature is required for a confirmed sell")
    confirmed = dict(record)
    confirmed.update({
        "token_mint": token_mint,
        "status": "confirmed_awaiting_accounting",
        "signature": signature.strip(),
        "reconciliation_note": (note or "operator verified on-chain")[:500],
    })
    pending[token_mint] = confirmed
    unresolved.pop(token_mint, None)
    unaccounted[token_mint] = dict(confirmed)
