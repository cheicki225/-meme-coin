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
