import asyncio
import sqlite3

import wallet_history

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



def test_signature_fetch_distinguishes_valid_empty_from_rpc_failure(monkeypatch):
    calls = {"count": 0}

    async def empty_rpc(*args, **kwargs):
        calls["count"] += 1
        return []

    monkeypatch.setattr(wallet_history.rpc_client, "rpc_post", empty_rpc)
    assert asyncio.run(wallet_history._get_signatures("Wallet", max_retries=2, raise_on_error=True)) == []
    assert calls["count"] == 1


def test_signature_fetch_raises_after_invalid_rpc_responses(monkeypatch):
    async def invalid_rpc(*args, **kwargs):
        return None

    async def no_wait(*args, **kwargs):
        return None

    monkeypatch.setattr(wallet_history.rpc_client, "rpc_post", invalid_rpc)
    monkeypatch.setattr(wallet_history.asyncio, "sleep", no_wait)
    try:
        asyncio.run(wallet_history._get_signatures("Wallet", max_retries=2, raise_on_error=True))
    except RuntimeError as exc:
        assert "failed after 2 attempts" in str(exc)
    else:
        raise AssertionError("RPC failure must not be converted to empty history")


def test_collect_wallet_reports_signature_rpc_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(collector.config, "HELIUS_API_KEY", "test-key")

    async def failed_signatures(*args, **kwargs):
        raise RuntimeError("RPC unavailable")

    monkeypatch.setattr(collector.wallet_history, "_get_signatures", failed_signatures)
    result = asyncio.run(collector.collect_wallet(WALLET, db_path=tmp_path / "wallets.sqlite3"))
    assert result["status"] == "error"
    assert result["reason"] == "signature_fetch_failed"


def test_collect_wallet_reports_valid_empty_history(monkeypatch, tmp_path):
    monkeypatch.setattr(collector.config, "HELIUS_API_KEY", "test-key")

    async def empty_signatures(*args, **kwargs):
        return []

    monkeypatch.setattr(collector.wallet_history, "_get_signatures", empty_signatures)
    result = asyncio.run(collector.collect_wallet(WALLET, db_path=tmp_path / "wallets.sqlite3"))
    assert result["status"] == "ok"
    assert result["signatures_scanned"] == 0
    assert result["events_detected"] == 0
