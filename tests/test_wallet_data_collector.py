import sqlite3

import wallet_data_collector as collector


WALLET = "WalletExample111"


def _tx(token_delta=100, sol_delta=-2_000_000_000):
    return {
        "blockTime": 123,
        "transaction": {"message": {
            "accountKeys": [WALLET, "Other"],
            "instructions": [{"programId": "PumpProgram"}],
        }},
        "meta": {
            "err": None, "fee": 5000,
            "preBalances": [10_000_000_000, 0],
            "postBalances": [10_000_000_000 + sol_delta, 0],
            "preTokenBalances": [{"owner": WALLET, "mint": "MintA", "uiTokenAmount": {"amount": "0", "decimals": 6}}],
            "postTokenBalances": [{"owner": WALLET, "mint": "MintA", "uiTokenAmount": {"amount": str(token_delta), "decimals": 6}}],
        },
    }


def test_normalize_buy_candidate(monkeypatch):
    monkeypatch.setattr(collector.config, "PUMP_FUN_PROGRAM_ID", "PumpProgram")
    result = collector.normalize_transaction(WALLET, "sig1", _tx())
    assert result["events"][0]["event_type"] == "buy_candidate"
    assert result["events"][0]["sol_delta"] < 0
    assert result["events"][0]["realized_pnl_sol"] is None


def test_unbalanced_transfer_is_not_classified_as_trade(monkeypatch):
    monkeypatch.setattr(collector.config, "PUMP_FUN_PROGRAM_ID", "PumpProgram")
    result = collector.normalize_transaction(WALLET, "sig1", _tx(sol_delta=100_000_000))
    assert result["events"][0]["event_type"] == "unclassified_movement"
    assert result["events"][0]["classification_confidence"] == "low"


def test_failed_transaction_is_ignored():
    tx = _tx()
    tx["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    assert collector.normalize_transaction(WALLET, "sig1", tx) is None


def test_store_is_idempotent_and_pnl_stays_unknown(tmp_path):
    event = {
        "signature": "sig1", "wallet_address": WALLET, "token_mint": "MintA",
        "block_time": 123, "event_type": "buy_candidate", "token_delta_raw": 100,
        "token_decimals": 6, "sol_delta": -2.0,
        "classification_confidence": "candidate", "source": "test",
    }
    path = tmp_path / "wallets.sqlite3"
    assert collector.store_events([event], path) == 1
    assert collector.store_events([event], path) == 0
    rows = collector.read_wallet_activity(WALLET, path)
    assert len(rows) == 1
    assert rows[0]["realized_pnl_sol"] is None
    assert rows[0]["entry_price_sol"] is None
