"""Safely inspect persisted swap signatures after a restart.

Chain inspection is read-only. A missing or non-finalized status is never
treated as failure, and a finalized success without durable position
accounting stays blocked for operator reconciliation.
"""

from __future__ import annotations

from typing import Any, Callable


def classify_pending_transactions(state: dict) -> tuple[list[str], list[dict]]:
    """Return signatures already attached to positions and unresolved records.

    A transaction may have been confirmed just before a process crash. If the
    signature is present in a persisted position, the position state is the
    recovery record. Any other pending signature remains ambiguous and must
    keep execution halted for manual reconciliation.
    """
    if not isinstance(state, dict):
        return [], [{"signature": None, "reason": "invalid_state_root"}]

    known = set()
    for bucket in ("open_positions", "closed_positions"):
        positions = state.get(bucket, [])
        if not isinstance(positions, list):
            continue
        for position in positions:
            if not isinstance(position, dict):
                continue
            signatures = position.get("tx_signatures", [])
            if isinstance(signatures, (list, tuple)):
                known.update(
                    signature for signature in signatures
                    if isinstance(signature, str) and signature
                )

    reconciled = []
    unresolved = []
    pending = state.get("pending_transactions", [])
    if pending is None:
        return reconciled, [{"signature": None, "reason": "invalid_pending_transactions"}]
    if not isinstance(pending, list):
        return reconciled, [{"signature": None, "reason": "invalid_pending_transactions"}]

    for record in pending:
        if (not isinstance(record, dict)
                or not isinstance(record.get("signature"), str)
                or not record.get("signature")):
            unresolved.append(record if isinstance(record, dict) else {"signature": None})
        elif record["signature"] in known:
            reconciled.append(record["signature"])
        else:
            unresolved.append(record)
    return reconciled, unresolved


async def inspect_signature_on_chain(
    signature: str,
    *,
    rpc_post: Callable[..., Any] | None = None,
) -> dict:
    """Read a signature's finalized status and fee without submitting anything."""
    if not isinstance(signature, str) or not signature.strip() or len(signature) > 100:
        return {"status": "invalid_signature"}
    if rpc_post is None:
        import rpc_client
        rpc_post = rpc_client.rpc_post

    try:
        status_result = await rpc_post({
            "jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses",
            "params": [[signature], {"searchTransactionHistory": True}],
        }, timeout=10)
    except Exception as exc:
        return {"status": "unknown", "reason": type(exc).__name__}

    values = status_result.get("value") if isinstance(status_result, dict) else None
    status = values[0] if isinstance(values, list) and values else None
    if not isinstance(status, dict):
        return {"status": "unknown", "reason": "signature_not_found"}
    if status.get("confirmationStatus") != "finalized":
        return {"status": "pending", "reason": "not_finalized"}

    try:
        transaction = await rpc_post({
            "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
            "params": [signature, {
                "encoding": "json", "commitment": "finalized",
                "maxSupportedTransactionVersion": 0,
            }],
        }, timeout=15)
    except Exception as exc:
        return {"status": "unknown", "reason": type(exc).__name__}

    meta = transaction.get("meta") if isinstance(transaction, dict) else None
    if not isinstance(meta, dict):
        return {"status": "unknown", "reason": "finalized_transaction_unavailable"}
    fee = meta.get("fee")
    if not isinstance(fee, int) or isinstance(fee, bool) or fee < 0:
        return {"status": "unknown", "reason": "invalid_finalized_fee"}

    failed = status.get("err") is not None or meta.get("err") is not None
    if failed:
        return {"status": "finalized_failed", "fee_lamports": fee}
    return {"status": "finalized_success", "fee_lamports": fee}


async def reconcile_pending_transactions_readonly(
    data_store,
    *,
    inspector: Callable[[str], Any] | None = None,
    max_checks: int = 20,
) -> dict:
    """Resolve only local matches and finalized failed swaps; leave other cases blocked."""
    if inspector is None:
        inspector = inspect_signature_on_chain
    if not isinstance(max_checks, int) or isinstance(max_checks, bool) or max_checks < 0:
        raise ValueError("max_checks must be a non-negative integer")

    reconciled, unresolved = classify_pending_transactions(data_store.state)
    resolved_local = 0
    for signature in reconciled:
        if data_store.resolve_pending_transaction(
            signature, {"chain_status": "already_attached_to_persisted_position"}
        ):
            resolved_local += 1

    checked = 0
    failed = 0
    confirmed_unaccounted = 0
    pending_or_unknown = 0
    invalid = 0
    for item in unresolved:
        signature = item.get("signature") if isinstance(item, dict) else None
        if not isinstance(signature, str) or not signature:
            invalid += 1
            continue
        if checked >= max_checks:
            pending_or_unknown += 1
            continue
        checked += 1
        try:
            result = await inspector(signature)
        except Exception as exc:
            result = {"status": "unknown", "reason": type(exc).__name__}
        result = result if isinstance(result, dict) else {"status": "unknown"}
        status = result.get("status")
        if status == "finalized_failed":
            fee = result.get("fee_lamports")
            if isinstance(fee, int) and not isinstance(fee, bool) and fee >= 0:
                if data_store.resolve_pending_transaction(signature, {
                    "chain_status": "finalized_failed",
                    "fee_lamports": fee,
                    "fee_included_in_pnl": False,
                }):
                    failed += 1
                else:
                    pending_or_unknown += 1
            else:
                pending_or_unknown += 1
        elif status == "finalized_success":
            # A chain success without a persisted position cannot be safely
            # converted into a position from the signature alone. Keep it
            # pending and require reconciliation of actual balance deltas.
            confirmed_unaccounted += 1
        elif status == "invalid_signature":
            invalid += 1
        else:
            pending_or_unknown += 1

    remaining = len(data_store.state.get("pending_transactions", []))
    return {
        "checked": checked,
        "resolved_from_positions": resolved_local,
        "finalized_failures_resolved": failed,
        "confirmed_unaccounted": confirmed_unaccounted,
        "pending_or_unknown": pending_or_unknown,
        "invalid": invalid,
        "deferred": max(0, len(unresolved) - max_checks),
        "remaining": remaining,
    }

