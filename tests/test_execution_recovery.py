from execution_recovery import classify_pending_transactions


def test_reconciles_signatures_attached_to_persisted_positions():
    state = {
        "open_positions": [{"tx_signatures": ["buy-1"]}],
        "closed_positions": [{"tx_signatures": ["buy-2", "sell-2"]}],
        "pending_transactions": [
            {"signature": "buy-1", "status": "accounted"},
            {"signature": "sell-2", "status": "accounted"},
            {"signature": "unknown", "status": "submitted"},
        ],
    }
    reconciled, unresolved = classify_pending_transactions(state)
    assert reconciled == ["buy-1", "sell-2"]
    assert [item["signature"] for item in unresolved] == ["unknown"]


def test_malformed_pending_records_remain_unresolved():
    reconciled, unresolved = classify_pending_transactions({"pending_transactions": [None, {}]})
    assert reconciled == []
    assert len(unresolved) == 2

