import asyncio

import config
import wallet_data_collector as collector
import wallet_history


WALLET = "Wallet111111111111111111111111111111111111"
MINT = "Token1111111111111111111111111111111111111"


def test_token_deltas_sum_multiple_wallet_accounts_by_mint():
    tx = {"meta": {
        "preTokenBalances": [
            {"owner": WALLET, "mint": MINT, "uiTokenAmount": {"amount": "2", "decimals": 6}},
            {"owner": WALLET, "mint": MINT, "uiTokenAmount": {"amount": "3", "decimals": 6}},
        ],
        "postTokenBalances": [
            {"owner": WALLET, "mint": MINT, "uiTokenAmount": {"amount": "10", "decimals": 6}},
        ],
    }}
    assert collector._wallet_token_deltas(tx, WALLET) == [
        {"mint": MINT, "raw_delta": 5, "decimals": 6}
    ]


def test_sqlite_events_are_idempotent_and_cursor_survives_restart(tmp_path):
    event = {
        "signature": "sig-1", "wallet_address": WALLET, "token_mint": MINT,
        "block_time": 1, "event_type": "buy_candidate", "token_delta_raw": 4,
        "token_decimals": 6, "sol_delta": -0.1,
        "classification_confidence": "candidate", "source": "test",
    }
    path = tmp_path / "wallets.sqlite3"
    assert collector.store_events([event], path) == 1
    assert collector.store_events([event], path) == 0
    collector.set_collection_cursor(WALLET, "sig-1", path)
    assert collector.get_collection_cursor(WALLET, path) == "sig-1"
    assert collector.read_wallet_activity(WALLET, path)[0]["token_delta_raw"] == "4"


def test_collector_advances_cursor_only_after_all_transactions_are_available(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HELIUS_API_KEY", "configured")
    monkeypatch.setattr(wallet_history, "_get_signatures", _signatures)
    monkeypatch.setattr(wallet_history, "_get_raw_transaction", _transaction)
    monkeypatch.setattr(collector, "normalize_transaction", lambda wallet, signature, tx: None)

    result = asyncio.run(collector.collect_wallet(WALLET, limit=5, db_path=tmp_path / "events.sqlite3"))
    assert result["status"] == "ok"
    assert collector.get_collection_cursor(WALLET, tmp_path / "events.sqlite3") == "newest"

    async def failed_transaction(signature):
        return None

    monkeypatch.setattr(wallet_history, "_get_raw_transaction", failed_transaction)
    result = asyncio.run(collector.collect_wallet(WALLET, limit=5, db_path=tmp_path / "events.sqlite3"))
    assert result["status"] == "partial"
    assert collector.get_collection_cursor(WALLET, tmp_path / "events.sqlite3") == "newest"


async def _signatures(wallet, limit, until=None):
    return [{"signature": "newest"}, {"signature": "older"}]


async def _transaction(signature):
    return {"meta": {"err": None}}

