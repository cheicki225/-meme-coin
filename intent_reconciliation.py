"""Helpers for safely reviewing unresolved live order intents.

This module is intentionally network-free: reconciliation must be performed by
an operator or a provider-specific adapter that can verify chain state.
"""

from __future__ import annotations

import copy
import math
import time
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


def list_unaccounted_confirmed_buys(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """List confirmed LIVE buys whose position accounting is not durable yet."""
    intents = state.get("safety_order_intents", {})
    recovery = state.get("safety_unaccounted_confirmed_buys", {})
    if not isinstance(intents, dict) or not isinstance(recovery, dict):
        raise ValueError("Buy reconciliation registry is corrupted")

    items: list[dict[str, Any]] = []
    for key, record in intents.items():
        if not isinstance(record, dict):
            continue
        if (record.get("side") == "buy" and record.get("status") == "confirmed"
                and record.get("accounting_status") != "accounted"):
            item = dict(record)
            item["idempotency_key"] = key
            item["recovery_record"] = next(
                (dict(value) for value in recovery.values()
                 if isinstance(value, dict) and value.get("intent_idempotency_key") == key),
                None,
            )
            items.append(item)
    def _created_at(item):
        try:
            value = float(item.get("created_at", 0) or 0)
            return value if math.isfinite(value) else 0.0
        except (TypeError, ValueError, OverflowError):
            return 0.0

    return sorted(items, key=_created_at)


def resolve_confirmed_buy_accounting(
    state: dict[str, Any],
    idempotency_key: str,
    *,
    position: Mapping[str, Any],
    note: str,
) -> None:
    """Apply operator-verified chain/accounting data and persist a recovered position in state.

    The caller MUST durably save the state (prefer DataStore.save_or_raise)
    immediately after this function returns.
    """
    intents = state.get("safety_order_intents")
    recovery = state.get("safety_unaccounted_confirmed_buys")
    positions = state.setdefault("open_positions", [])
    if not isinstance(intents, dict) or not isinstance(recovery, dict) or not isinstance(positions, list):
        raise ValueError("Buy reconciliation state is missing or corrupted")
    record = intents.get(idempotency_key)
    if (not isinstance(record, dict) or record.get("side") != "buy"
            or record.get("status") != "confirmed"
            or record.get("accounting_status") == "accounted"):
        raise ValueError("Only confirmed, unaccounted LIVE buys can be reconciled")
    if not isinstance(position, Mapping):
        raise ValueError("A reconciled position is required")

    signature = record.get("signature")
    signatures = position.get("tx_signatures")
    if not isinstance(signature, str) or not signature.strip():
        raise ValueError("Confirmed buy signature is missing")
    if not isinstance(signatures, list) or signature not in signatures:
        raise ValueError("Reconciled position must include the confirmed transaction signature")
    if position.get("token_mint") != record.get("token_mint"):
        raise ValueError("Reconciled position token does not match the confirmed buy")
    if position.get("source_wallet") != record.get("source_wallet"):
        raise ValueError("Reconciled position wallet does not match the confirmed buy")

    for field in ("units", "cost_basis_usd", "entry_price"):
        try:
            value = float(position.get(field))
        except (TypeError, ValueError):
            raise ValueError(f"Invalid reconciled position {field}") from None
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"Invalid reconciled position {field}")

    existing_position = next(
        (
            item for item in positions
            if isinstance(item, dict)
            and isinstance(item.get("tx_signatures"), list)
            and signature in item["tx_signatures"]
        ),
        None,
    )
    if existing_position is not None:
        # Idempotency is valid only when the existing position belongs to
        # this exact mint and wallet. A reused/corrupt signature must not
        # silently mark a different position as accounted.
        if (
            existing_position.get("token_mint") != record.get("token_mint")
            or existing_position.get("source_wallet") != record.get("source_wallet")
        ):
            raise ValueError("Transaction signature already belongs to another position")
    else:
        positions.append(dict(position))

    record["accounting_status"] = "accounted"
    record["accounted_at"] = time.time()
    record["reconciliation_note"] = str(note)[:500]
    for key, item in list(recovery.items()):
        if (isinstance(item, dict)
                and (item.get("intent_idempotency_key") == idempotency_key
                     or item.get("signature") == signature)):
            recovery.pop(key, None)

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



def resolve_confirmed_buy_accounting_durable(
    data_store: Any,
    idempotency_key: str,
    *,
    position: Mapping[str, Any],
    note: str,
) -> None:
    """Resolve a confirmed buy and durably save it as one in-memory transaction.

    The store must expose state and save_or_raise(). If persistence raises,
    restore the in-memory snapshot so the running process cannot accidentally
    treat an unpersisted reconciliation as complete.
    """
    state = getattr(data_store, "state", None)
    saver = getattr(data_store, "save_or_raise", None)
    if not isinstance(state, dict) or not callable(saver):
        raise TypeError("A DataStore with state and save_or_raise() is required")

    snapshot = copy.deepcopy(state)
    try:
        resolve_confirmed_buy_accounting(
            state, idempotency_key, position=position, note=note,
        )
        saver()
    except Exception:
        state.clear()
        state.update(snapshot)
        raise
