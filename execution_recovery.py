"""Classify persisted swap signatures when the bot starts after a restart."""


def classify_pending_transactions(state: dict) -> tuple[list[str], list[dict]]:
    """Return signatures already attached to positions and unresolved records.

    A transaction may have been confirmed just before a process crash. If the
    signature is present in a persisted position, the position state is the
    recovery record. Any other pending signature remains ambiguous and must
    keep execution halted for manual reconciliation.
    """
    known = set()
    for bucket in ("open_positions", "closed_positions"):
        for position in state.get(bucket, []) or []:
            if isinstance(position, dict):
                known.update(signature for signature in position.get("tx_signatures", []) if signature)
    reconciled = []
    unresolved = []
    for record in state.get("pending_transactions", []) or []:
        if not isinstance(record, dict) or not record.get("signature"):
            unresolved.append(record if isinstance(record, dict) else {"signature": None})
        elif record["signature"] in known:
            reconciled.append(record["signature"])
        else:
            unresolved.append(record)
    return reconciled, unresolved

