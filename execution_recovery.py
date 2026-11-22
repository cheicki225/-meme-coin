"""Classify persisted swap signatures when the bot starts after a restart."""


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

