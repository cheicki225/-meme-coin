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
    assert len(collector.read_wallet_activity(WALLET, path, limit=1)) == 1


def test_read_wallet_activity_rejects_unbounded_or_malformed_limit(tmp_path):
    for invalid in (True, 0, -1, 1001, 1.5, "10"):
        try:
            collector.read_wallet_activity(WALLET, tmp_path / "empty.sqlite3", limit=invalid)
        except ValueError as exc:
            assert "limit" in str(exc)
        else:
            raise AssertionError("invalid read limit must be rejected")


def test_collector_works_with_public_http_rpc_and_advances_cursor_safely(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HELIUS_API_KEY", "")
    monkeypatch.setattr(wallet_history, "_get_signatures", _signatures)
    monkeypatch.setattr(wallet_history, "_get_raw_transaction", _transaction)
    monkeypatch.setattr(collector, "normalize_transaction", lambda wallet, signature, tx: None)

    result = asyncio.run(collector.collect_wallet(WALLET, limit=5, db_path=tmp_path / "events.sqlite3"))
    assert result["status"] == "ok"
    assert result["collection_scope"] == "bounded_signature_page"
    assert result["completeness"] == "current_page_processed"
    assert result["source"] == collector.COLLECTION_SOURCE
    assert result["cursor_advanced"] is True
    assert collector.get_collection_cursor(WALLET, tmp_path / "events.sqlite3") == "newest"

    async def failed_transaction(signature):
        return None

    monkeypatch.setattr(wallet_history, "_get_raw_transaction", failed_transaction)
    result = asyncio.run(collector.collect_wallet(WALLET, limit=5, db_path=tmp_path / "events.sqlite3"))
    assert result["status"] == "partial"
    assert result["reason"] == "transaction_fetch_partial"
    assert result["completeness"] == "partial"
    assert result["cursor_advanced"] is False
    assert collector.get_collection_cursor(WALLET, tmp_path / "events.sqlite3") == "newest"


def test_malformed_signature_record_is_partial_and_does_not_advance_cursor(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HELIUS_API_KEY", "")

    async def signatures(*args, **kwargs):
        return [{"signature": "newest"}, {}, {"signature": "older"}]

    monkeypatch.setattr(wallet_history, "_get_signatures", signatures)
    monkeypatch.setattr(wallet_history, "_get_raw_transaction", _transaction)
    monkeypatch.setattr(collector, "normalize_transaction", lambda *_args: None)

    path = tmp_path / "malformed.sqlite3"
    result = asyncio.run(collector.collect_wallet(WALLET, limit=5, db_path=path))

    assert result["status"] == "partial"
    assert result["reason"] == "invalid_signature_record"
    assert result["invalid_signature_records"] == 1
    assert result["completeness"] == "partial"
    assert result["cursor_advanced"] is False
    assert collector.get_collection_cursor(WALLET, path) is None


def test_transaction_without_metadata_is_partial_and_does_not_advance_cursor(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HELIUS_API_KEY", "")
    monkeypatch.setattr(wallet_history, "_get_signatures", _signatures)

    async def transaction_without_metadata(_signature):
        return {"transaction": {"message": {}}}

    monkeypatch.setattr(wallet_history, "_get_raw_transaction", transaction_without_metadata)
    path = tmp_path / "missing-meta.sqlite3"

    result = asyncio.run(collector.collect_wallet(WALLET, limit=5, db_path=path))

    assert result["status"] == "partial"
    assert result["reason"] == "transaction_fetch_partial"
    assert result["completeness"] == "partial"
    assert result["transactions_unavailable"] == 2
    assert result["cursor_advanced"] is False
    assert collector.get_collection_cursor(WALLET, path) is None


def test_pump_fun_null_token_balances_are_unknown_and_do_not_advance_cursor(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HELIUS_API_KEY", "")
    monkeypatch.setattr(wallet_history, "_get_signatures", _signatures)

    async def transaction_with_null_token_balances(_signature):
        return {"transaction": {"message": {"instructions": [{"programId": config.PUMP_FUN_PROGRAM_ID}]}}, "meta": {
            "innerInstructions": [],
            "err": None, "preTokenBalances": None, "postTokenBalances": None,
        }}

    monkeypatch.setattr(wallet_history, "_get_raw_transaction", transaction_with_null_token_balances)
    monkeypatch.setattr(wallet_history, "_transaction_involves_program", lambda *_args: True)
    path = tmp_path / "null-token-balances.sqlite3"

    result = asyncio.run(collector.collect_wallet(WALLET, limit=5, db_path=path))

    assert result["status"] == "partial"
    assert result["reason"] == "transaction_fetch_partial"
    assert result["completeness"] == "partial"
    assert result["transactions_unavailable"] == 2
    assert result["cursor_advanced"] is False
    assert collector.get_collection_cursor(WALLET, path) is None


def test_non_pump_transaction_without_token_balances_is_a_complete_no_event(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HELIUS_API_KEY", "")
    monkeypatch.setattr(wallet_history, "_get_signatures", _signatures)

    async def unrelated_transaction(_signature):
        return {"transaction": {"message": {"instructions": []}}, "meta": {
            "innerInstructions": [],
            "err": None, "preTokenBalances": None, "postTokenBalances": None,
        }}

    monkeypatch.setattr(wallet_history, "_get_raw_transaction", unrelated_transaction)
    monkeypatch.setattr(wallet_history, "_transaction_involves_program", lambda *_args: False)
    path = tmp_path / "unrelated-null-token-balances.sqlite3"

    result = asyncio.run(collector.collect_wallet(WALLET, limit=5, db_path=path))

    assert result["status"] == "ok"
    assert result["completeness"] == "current_page_processed"
    assert result["transactions_unavailable"] == 0
    assert result["cursor_advanced"] is True
    assert collector.get_collection_cursor(WALLET, path) == "newest"


def test_pump_fun_token_amount_over_u64_is_unknown_and_does_not_advance_cursor(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HELIUS_API_KEY", "")
    monkeypatch.setattr(wallet_history, "_get_signatures", _signatures)
    monkeypatch.setattr(wallet_history, "_transaction_involves_program", lambda *_args: True)

    async def transaction_with_oversized_amount(_signature):
        row = {
            "owner": WALLET, "mint": MINT,
            "uiTokenAmount": {"amount": "9" * 5000, "decimals": 6},
        }
        return {"transaction": {"message": {"instructions": [{"programId": config.PUMP_FUN_PROGRAM_ID}]}}, "meta": {
            "innerInstructions": [],
            "err": None, "preTokenBalances": [row], "postTokenBalances": [],
        }}

    monkeypatch.setattr(wallet_history, "_get_raw_transaction", transaction_with_oversized_amount)
    path = tmp_path / "oversized-token-amount.sqlite3"

    result = asyncio.run(collector.collect_wallet(WALLET, limit=5, db_path=path))

    assert result["status"] == "partial"
    assert result["completeness"] == "partial"
    assert result["transactions_unavailable"] == 2
    assert result["cursor_advanced"] is False
    assert collector.get_collection_cursor(WALLET, path) is None


def test_missing_or_malformed_instructions_are_partial_and_do_not_advance_cursor(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HELIUS_API_KEY", "")
    monkeypatch.setattr(wallet_history, "_get_signatures", _signatures)
    malformed_transactions = (
        {"transaction": {"message": {}}, "meta": {"err": None, "innerInstructions": []}},
        {
            "transaction": {"message": {"instructions": [{"programId": None}]}},
            "meta": {"err": None, "innerInstructions": []},
        },
        {
            "transaction": {"message": {"instructions": [{"programId": "not-a-solana-address"}]}},
            "meta": {"err": None, "innerInstructions": []},
        },
        {
            "transaction": {"message": {"instructions": [{"programId": "1" * 10_000}]}},
            "meta": {"err": None, "innerInstructions": []},
        },
    )

    for index, malformed in enumerate(malformed_transactions):
        async def transaction_response(_signature, response=malformed):
            return response

        monkeypatch.setattr(wallet_history, "_get_raw_transaction", transaction_response)
        path = tmp_path / f"malformed-instructions-{index}.sqlite3"
        result = asyncio.run(collector.collect_wallet(WALLET, limit=5, db_path=path))

        assert result["status"] == "partial"
        assert result["reason"] == "transaction_fetch_partial"
        assert result["completeness"] == "partial"
        assert result["cursor_advanced"] is False
        assert collector.get_collection_cursor(WALLET, path) is None


def test_signature_page_over_limit_fails_closed_before_transaction_fetch(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HELIUS_API_KEY", "")

    async def oversized_page(*args, **kwargs):
        return [{"signature": "one"}, {"signature": "two"}]

    async def unexpected_fetch(*args, **kwargs):
        raise AssertionError("an oversized signature page must not be processed")

    monkeypatch.setattr(wallet_history, "_get_signatures", oversized_page)
    monkeypatch.setattr(wallet_history, "_get_raw_transaction", unexpected_fetch)
    path = tmp_path / "oversized.sqlite3"

    result = asyncio.run(collector.collect_wallet(WALLET, limit=1, db_path=path))

    assert result["status"] == "error"
    assert result["reason"] == "signature_page_exceeds_limit"
    assert result["signature_limit"] == 1
    assert result["completeness"] == "unknown"
    assert result["cursor_advanced"] is False
    assert collector.get_collection_cursor(WALLET, path) is None


def test_collected_at_and_provenance_are_exposed_with_persisted_event(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HELIUS_API_KEY", "")
    monkeypatch.setattr(wallet_history, "_get_signatures", _signatures)
    monkeypatch.setattr(wallet_history, "_get_raw_transaction", _transaction)
    monkeypatch.setattr(collector.time, "time", lambda: 12345)
    event = {
        "wallet_address": WALLET, "signature": "newest", "token_mint": MINT,
        "block_time": None, "event_type": "unclassified_movement",
        "token_delta_raw": 1, "token_decimals": 6, "sol_delta": None,
        "classification_confidence": "low", "source": collector.COLLECTION_SOURCE,
    }
    monkeypatch.setattr(collector, "normalize_transaction", lambda *_args: {"events": [event.copy()]})
    path = tmp_path / "provenance.sqlite3"

    result = asyncio.run(collector.collect_wallet(WALLET, limit=5, db_path=path))
    stored = collector.read_wallet_activity(WALLET, path)[0]

    assert result["freshness_basis"].startswith("local_collected_at")
    assert result["collected_at"] == 12345
    assert result["events"][0]["collected_at"] == 12345
    assert stored["collected_at"] == 12345
    assert stored["source"] == collector.COLLECTION_SOURCE
    assert stored["block_time"] is None
    assert stored["realized_pnl_sol"] is None


def test_cursor_persistence_failure_is_reported_without_claiming_completeness(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HELIUS_API_KEY", "")
    monkeypatch.setattr(wallet_history, "_get_signatures", _signatures)
    monkeypatch.setattr(wallet_history, "_get_raw_transaction", _transaction)
    monkeypatch.setattr(collector, "normalize_transaction", lambda *_args: None)

    def fail_cursor(*_args, **_kwargs):
        raise OSError("simulated cursor storage failure")

    monkeypatch.setattr(collector, "set_collection_cursor", fail_cursor)
    path = tmp_path / "cursor-failure.sqlite3"

    result = asyncio.run(collector.collect_wallet(WALLET, limit=5, db_path=path))

    assert result["status"] == "error"
    assert result["reason"] == "storage_failed"
    assert result["completeness"] == "unknown"
    assert result["cursor_advanced"] is False
    assert collector.get_collection_cursor(WALLET, path) is None


def test_collector_reports_rpc_failure_instead_of_empty_history(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HELIUS_API_KEY", "")

    async def rpc_failure(*args, **kwargs):
        raise wallet_history.rpc_client.RpcUnavailableError("RPC unavailable")

    monkeypatch.setattr(wallet_history, "_get_signatures", rpc_failure)
    result = asyncio.run(collector.collect_wallet(WALLET, limit=5, db_path=tmp_path / "failed.sqlite3"))
    assert result["status"] == "error"
    assert result["reason"] == "signature_fetch_failed"
    assert result["completeness"] == "unknown"
    assert result["cursor_advanced"] is False


def test_empty_rpc_history_is_a_successful_empty_collection(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HELIUS_API_KEY", "")

    async def no_activity(*args, **kwargs):
        return []

    monkeypatch.setattr(wallet_history, "_get_signatures", no_activity)
    result = asyncio.run(collector.collect_wallet(WALLET, limit=5, db_path=tmp_path / "empty.sqlite3"))
    assert result["status"] == "ok"
    assert result["events_detected"] == 0
    assert result["completeness"] == "empty_page"
    assert result["cursor_advanced"] is False


async def _signatures(wallet, limit, until=None, strict=False, max_retries=4):
    return [{"signature": "newest"}, {"signature": "older"}]


async def _transaction(signature):
    return {"meta": {"err": None}}

