import multiprocessing
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import config
import transaction_accounting
from transaction_accounting import (
    AccountingError,
    account_confirmed_sol_transfer,
    account_confirmed_swap,
    value_confirmed_sol_cashflow_usd,
)
from monitoring_list import (
    MAX_CONFIRMED_ACCOUNTING_RECORDS,
    DataStore,
    _accounting_fingerprint,
    _accounting_signature_key,
)

SOL = "So11111111111111111111111111111111111111112"
TOKEN = "Token1111111111111111111111111111111111111"
WALLET = "Wallet111111111111111111111111111111111111"


def _record_accounting_in_process(database_path, legacy_path, barrier, result_queue, signature, timestamp):
    """Open the same SQLite state from a spawned process and record one fixture."""
    config.DATA_BACKEND = "sqlite"
    config.SQLITE_DB_FILE = str(database_path)
    config.DATA_FILE = str(legacy_path)
    store = DataStore()
    barrier.wait(timeout=20)
    accounting = _confirmed_accounting(event_timestamp=timestamp)
    valuation = value_confirmed_sol_cashflow_usd(accounting)
    inserted = store.record_confirmed_accounting(signature, accounting, valuation)
    result_queue.put(("ok", inserted))


def _transaction(*, native_delta=-1_010_000_000, token_delta=2_000_000, fee=10_000_000, extra=None):
    pre = [{"accountIndex": 1, "mint": TOKEN, "owner": WALLET,
            "uiTokenAmount": {"amount": "0", "decimals": 6}}]
    post = [{"accountIndex": 1, "mint": TOKEN, "owner": WALLET,
             "uiTokenAmount": {"amount": str(token_delta), "decimals": 6}}]
    if extra:
        post.append({"accountIndex": 2, "mint": extra, "owner": WALLET,
                     "uiTokenAmount": {"amount": "1", "decimals": 6}})
    return {
        "transaction": {"message": {"accountKeys": [WALLET, "TokenAccount"]}},
        "meta": {"err": None, "fee": fee,
                 "preBalances": [2_000_000_000, 0],
                 "postBalances": [2_000_000_000 + native_delta, 0],
                 "preTokenBalances": pre, "postTokenBalances": post},
    }


def test_buy_uses_confirmed_token_delta_and_fee_inclusive_sol_cashflow():
    result = account_confirmed_swap(WALLET, SOL, TOKEN, _transaction())
    assert result["native_delta_lamports"] == -1_010_000_000
    assert result["fee_lamports"] == 10_000_000
    assert result["output_delta_raw"] == 2_000_000
    assert result["token_decimals"][TOKEN] == 6


def test_sell_uses_actual_token_delta_and_net_sol_cashflow():
    tx = _transaction(native_delta=1_190_000_000, token_delta=0)
    tx["meta"]["preTokenBalances"][0]["uiTokenAmount"]["amount"] = "2000000"
    tx["meta"]["postTokenBalances"] = []
    result = account_confirmed_swap(WALLET, TOKEN, SOL, tx)
    assert result["native_delta_lamports"] == 1_190_000_000
    assert result["input_delta_raw"] == -2_000_000


def test_rejects_unrelated_wallet_token_movement():
    with pytest.raises(AccountingError, match="unrelated"):
        account_confirmed_swap(WALLET, SOL, TOKEN, _transaction(extra="OtherMint"))


def test_rejects_unconfirmed_or_failed_transaction_metadata():
    tx = _transaction()
    tx["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    with pytest.raises(AccountingError, match="failed"):
        account_confirmed_swap(WALLET, SOL, TOKEN, tx)


@pytest.mark.parametrize("value", [True, 1.9, "١", "-1", "1.0"])
def test_rejects_non_integer_token_amount_metadata(value):
    tx = _transaction()
    tx["meta"]["postTokenBalances"][0]["uiTokenAmount"]["amount"] = value
    with pytest.raises(AccountingError, match="malformed"):
        account_confirmed_swap(WALLET, SOL, TOKEN, tx)


@pytest.mark.parametrize("value", [True, 1.9, "١", "-1", "1.0"])
def test_rejects_non_integer_fee_and_lamport_balance_metadata(value):
    tx = _transaction()
    tx["meta"]["fee"] = value
    with pytest.raises(AccountingError, match="malformed"):
        account_confirmed_swap(WALLET, SOL, TOKEN, tx)

    tx = _transaction()
    tx["meta"]["preBalances"][0] = value
    with pytest.raises(AccountingError, match="malformed"):
        account_confirmed_swap(WALLET, SOL, TOKEN, tx)


def test_accepts_ascii_digit_strings_for_rpc_integer_fields():
    tx = _transaction()
    tx["meta"]["fee"] = "10000000"
    tx["meta"]["preBalances"][0] = "2000000000"
    tx["meta"]["postBalances"][0] = "990000000"
    result = account_confirmed_swap(WALLET, SOL, TOKEN, tx)
    assert result["fee_lamports"] == 10_000_000
    assert result["native_delta_lamports"] == -1_010_000_000


def test_verifies_manual_sol_transfer_destination_amount_and_fee():
    destination = "Destination111111111111111111111111111111"
    transaction = {
        "transaction": {"message": {"accountKeys": [WALLET, destination]}},
        "meta": {"err": None, "fee": 5_000, "preBalances": [2_000_000, 0],
                 "postBalances": [995_000, 1_000_000]},
    }
    result = account_confirmed_sol_transfer(WALLET, destination, 1_000_000, transaction)
    assert result["fee_lamports"] == 5_000
    assert result["destination_delta_lamports"] == 1_000_000

    transaction["meta"]["postBalances"][1] = 999_999
    with pytest.raises(AccountingError, match="deltas"):
        account_confirmed_sol_transfer(WALLET, destination, 1_000_000, transaction)


def _confirmed_accounting(**overrides):
    return {
        "accounting_schema_version": 1,
        "accounting_source": "confirmed_rpc_transaction_meta",
        "native_delta_lamports": -1_010_000_000,
        "fee_lamports": 10_000_000,
        "event_timestamp": 1_010.0,
        **overrides,
    }


def _value(accounting=None, **overrides):
    return value_confirmed_sol_cashflow_usd(
        accounting or _confirmed_accounting(),
        sol_usd_rate=100.0,
        rate_timestamp=1_000.0,
        rate_source="verified_fixture_snapshot",
        rate_verified=True,
        max_rate_age_s=60,
        **overrides,
    )


def test_caller_attested_rate_is_not_provider_provenance():
    result = _value()

    assert result["valuation_status"] == "unknown"
    assert result["valuation_reason"] == "rate_origin_unverifiable"
    assert result["sol_cashflow_usd"] is None
    assert result["fee_usd"] is None
    assert result["fee_included_in_sol_cashflow"] is True
    assert result["rate_age_seconds"] == pytest.approx(10.0)
    assert result["sol_usd_rate_source"] == "verified_fixture_snapshot"
    assert result["sol_usd_rate_verified"] is False
    assert result["sol_usd_rate_caller_attested"] is True
    assert result["rate_origin_status"] == "unverifiable"


def test_confirmed_accounting_preserves_rpc_block_timestamp_when_present():
    transaction = _transaction()
    transaction["blockTime"] = 1_010

    result = account_confirmed_swap(WALLET, SOL, TOKEN, transaction)

    assert result["accounting_schema_version"] == 1
    assert result["event_timestamp"] == 1_010.0


@pytest.mark.parametrize(
    ("rate", "rate_timestamp", "event_timestamp", "verified", "source", "reason"),
    [
        (None, 1_000.0, 1_010.0, True, "fixture", "missing_or_invalid_rate"),
        (100.0, None, 1_010.0, True, "fixture", "missing_rate_timestamp"),
        (100.0, 900.0, 1_010.0, True, "fixture", "stale_rate"),
        (100.0, 1_011.0, 1_010.0, True, "fixture", "rate_after_accounting_event"),
        (100.0, 1_000.0, 1_010.0, False, "fixture", "rate_not_verified"),
        (100.0, 1_000.0, 1_010.0, True, " ", "missing_rate_source"),
        (100.0, 1_000.0, 1_010.0, True, "fixture", "rate_origin_unverifiable"),
    ],
)
def test_missing_stale_future_or_unverified_rate_stays_unknown(
    rate, rate_timestamp, event_timestamp, verified, source, reason,
):
    result = value_confirmed_sol_cashflow_usd(
        _confirmed_accounting(event_timestamp=event_timestamp),
        sol_usd_rate=rate,
        rate_timestamp=rate_timestamp,
        rate_source=source,
        rate_verified=verified,
        max_rate_age_s=60,
    )

    assert result["valuation_status"] == "unknown"
    assert result["valuation_reason"] == reason
    assert result["sol_cashflow_usd"] is None
    assert result["fee_usd"] is None


def test_valuation_refuses_unconfirmed_source_and_boolean_metadata():
    assert _value(_confirmed_accounting(accounting_source="paper_estimate"))["valuation_reason"] == (
        "unconfirmed_accounting_source"
    )
    assert _value(_confirmed_accounting(accounting_schema_version=2))["valuation_reason"] == (
        "unsupported_accounting_schema"
    )
    assert _value(_confirmed_accounting(native_delta_lamports=True))["valuation_reason"] == (
        "invalid_native_delta"
    )
    assert _value(_confirmed_accounting(fee_lamports=True))["valuation_reason"] == (
        "invalid_confirmed_fee"
    )


def test_confirmed_accounting_ledger_persists_idempotently_without_changing_pnl(tmp_path):
    path = tmp_path / "state.json"
    store = DataStore(path=str(path))
    accounting = _confirmed_accounting()
    valuation = _value(accounting)

    assert store.record_confirmed_accounting("fixture-signature", accounting, valuation) is True
    assert store.record_confirmed_accounting("fixture-signature", accounting, valuation) is False
    assert store.state["total_pnl_usd"] == 0.0
    assert store.state["open_positions"] == []

    restarted = DataStore(path=str(path))
    assert restarted.state["confirmed_accounting_ledger"] == [
        {
            "signature": "fixture-signature",
            "accounting_fingerprint": _accounting_fingerprint(accounting),
            "accounting_schema_version": 1,
            "valuation_schema_version": 2,
            "accounting_source": "confirmed_rpc_transaction_meta",
            "native_delta_lamports": -1_010_000_000,
            "fee_lamports": 10_000_000,
            "fee_status": "confirmed",
            "fee_currency": "SOL",
            "fee_included_in_sol_cashflow": True,
            "accounting_domain": "CONFIRMED_CHAIN",
            "applied_to_paper_pnl": False,
            "event_timestamp": 1_010.0,
            "valuation_status": "unknown",
            "valuation_reason": "rate_origin_unverifiable",
            "sol_cashflow_usd": None,
            "fee_usd": None,
            "sol_usd_rate": 100.0,
            "sol_usd_rate_timestamp": 1_000.0,
            "sol_usd_rate_source": "verified_fixture_snapshot",
            "sol_usd_rate_verified": False,
            "sol_usd_rate_caller_attested": True,
            "rate_origin_status": "unverifiable",
            "max_rate_age_s": 60.0,
            "rate_age_seconds": 10.0,
        }
    ]
    assert restarted.state["confirmed_accounting_idempotency"] == {
        _accounting_signature_key("fixture-signature"): _accounting_fingerprint(accounting)
    }


def test_legacy_retained_ledger_row_is_backfilled_before_future_eviction(tmp_path):
    path = tmp_path / "legacy-state.json"
    store = DataStore(path=str(path))
    accounting = _confirmed_accounting()
    record = {
        "signature": "legacy-fixture-signature",
        "accounting_source": "confirmed_rpc_transaction_meta",
        "native_delta_lamports": accounting["native_delta_lamports"],
        "fee_lamports": accounting["fee_lamports"],
        "event_timestamp": accounting["event_timestamp"],
        "valuation_status": "unknown",
    }
    store.state["confirmed_accounting_ledger"] = [record]
    store.state.pop("confirmed_accounting_idempotency", None)
    store.save()

    upgraded = DataStore(path=str(path))
    assert upgraded.record_confirmed_accounting(
        "legacy-fixture-signature", accounting, value_confirmed_sol_cashflow_usd(accounting)
    ) is False
    assert "accounting_fingerprint" not in upgraded.state["confirmed_accounting_ledger"][0]
    assert upgraded.state["confirmed_accounting_idempotency"] == {
        _accounting_signature_key("legacy-fixture-signature"): None
    }
    conflicting_hidden_fields = _confirmed_accounting(wallet="different-wallet")
    assert upgraded.record_confirmed_accounting(
        "legacy-fixture-signature",
        conflicting_hidden_fields,
        value_confirmed_sol_cashflow_usd(conflicting_hidden_fields),
    ) is False
    restarted = DataStore(path=str(path))
    assert restarted.record_confirmed_accounting(
        "legacy-fixture-signature", accounting, value_confirmed_sol_cashflow_usd(accounting)
    ) is False


def test_unknown_valuation_is_persisted_and_caller_claim_cannot_reconcile_it(tmp_path):
    store = DataStore(path=str(tmp_path / "state.json"))
    accounting = _confirmed_accounting()
    unknown = value_confirmed_sol_cashflow_usd(accounting)

    assert store.record_confirmed_accounting("fixture-signature", accounting, unknown) is True
    assert store.state["confirmed_accounting_ledger"][0]["sol_cashflow_usd"] is None
    assert store.state["confirmed_accounting_ledger"][0]["fee_usd"] is None

    assert store.record_confirmed_accounting("fixture-signature", accounting, _value(accounting)) is False
    assert store.state["confirmed_accounting_ledger"][0]["valuation_status"] == "unknown"
    assert store.state["confirmed_accounting_ledger"][0]["sol_cashflow_usd"] is None


def test_confirmed_ledger_rolls_back_when_persistence_fails(monkeypatch):
    store = object.__new__(DataStore)
    store.state = {"confirmed_accounting_ledger": []}

    def fail_save():
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "save", fail_save)
    with pytest.raises(OSError, match="disk unavailable"):
        store.record_confirmed_accounting(
            "fixture-signature", _confirmed_accounting(), _value()
        )

    assert store.state["confirmed_accounting_ledger"] == []
    assert "confirmed_accounting_idempotency" not in store.state


def test_confirmed_ledger_rejects_conflicting_fee_and_unverified_usd_values(monkeypatch):
    store = object.__new__(DataStore)
    store.state = {"confirmed_accounting_ledger": []}
    monkeypatch.setattr(store, "save", lambda: None)
    accounting = _confirmed_accounting()
    assert store.record_confirmed_accounting(
        "fixture-signature", accounting, _value(accounting)
    ) is True

    with pytest.raises(ValueError, match="Conflicting confirmed accounting"):
        changed_accounting = _confirmed_accounting(fee_lamports=5)
        store.record_confirmed_accounting(
            "fixture-signature", changed_accounting, _value(changed_accounting)
        )

    unknown = value_confirmed_sol_cashflow_usd(accounting)
    unknown["sol_cashflow_usd"] = 12.0
    with pytest.raises(ValueError, match="does not match confirmed"):
        store.record_confirmed_accounting("other-signature", accounting, unknown)


def test_confirmed_ledger_recomputes_values_before_persisting(monkeypatch):
    store = object.__new__(DataStore)
    store.state = {"confirmed_accounting_ledger": []}
    monkeypatch.setattr(store, "save", lambda: None)
    accounting = _confirmed_accounting()
    valuation = _value(accounting)
    valuation["fee_usd"] = 3.0

    with pytest.raises(ValueError, match="does not match confirmed"):
        store.record_confirmed_accounting("fixture-signature", accounting, valuation)
    assert store.state["confirmed_accounting_ledger"] == []


def test_confirmed_ledger_rejects_unsupported_accounting_schema(monkeypatch):
    store = object.__new__(DataStore)
    store.state = {"confirmed_accounting_ledger": []}
    monkeypatch.setattr(store, "save", lambda: None)
    accounting = _confirmed_accounting(accounting_schema_version=2)
    valuation = value_confirmed_sol_cashflow_usd(accounting)

    with pytest.raises(ValueError, match="Unsupported confirmed accounting schema"):
        store.record_confirmed_accounting("fixture-signature", accounting, valuation)
    assert store.state["confirmed_accounting_ledger"] == []


def test_valuation_rejects_caller_supplied_freshness_window_over_limit():
    result = value_confirmed_sol_cashflow_usd(
        _confirmed_accounting(),
        sol_usd_rate=100.0,
        rate_timestamp=1_000.0,
        rate_source="verified_fixture_snapshot",
        rate_verified=True,
        max_rate_age_s=301,
    )

    assert result["valuation_status"] == "unknown"
    assert result["valuation_reason"] == "invalid_max_rate_age"


def test_confirmed_accounting_replay_after_eviction_and_restart_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    store = DataStore(path=str(path))
    save = store.save
    monkeypatch.setattr(store, "save", lambda: None)
    first_accounting = _confirmed_accounting(event_timestamp=1_010.0)

    for index in range(MAX_CONFIRMED_ACCOUNTING_RECORDS + 1):
        accounting = _confirmed_accounting(event_timestamp=1_010.0 + index)
        unknown = value_confirmed_sol_cashflow_usd(accounting)
        assert store.record_confirmed_accounting(f"fixture-signature-{index}", accounting, unknown)

    assert len(store.state["confirmed_accounting_ledger"]) == MAX_CONFIRMED_ACCOUNTING_RECORDS
    assert all(row["signature"] != "fixture-signature-0" for row in store.state["confirmed_accounting_ledger"])
    assert len(store.state["confirmed_accounting_idempotency"]) == MAX_CONFIRMED_ACCOUNTING_RECORDS + 1

    monkeypatch.setattr(store, "save", save)
    store.save()
    restarted = DataStore(path=str(path))
    assert restarted.record_confirmed_accounting(
        "fixture-signature-0", first_accounting, value_confirmed_sol_cashflow_usd(first_accounting)
    ) is False
    assert len(restarted.state["confirmed_accounting_ledger"]) == MAX_CONFIRMED_ACCOUNTING_RECORDS

    conflicting = _confirmed_accounting(event_timestamp=9_999.0)
    with pytest.raises(ValueError, match="Conflicting confirmed accounting"):
        restarted.record_confirmed_accounting(
            "fixture-signature-0", conflicting, value_confirmed_sol_cashflow_usd(conflicting)
        )


def test_concurrent_confirmed_accounting_replays_insert_once(tmp_path):
    store = DataStore(path=str(tmp_path / "state.json"))
    accounting = _confirmed_accounting()
    valuation = value_confirmed_sol_cashflow_usd(accounting)

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(
            lambda _: store.record_confirmed_accounting("concurrent-fixture-signature", accounting, valuation),
            range(24),
        ))

    assert sum(results) == 1
    assert len(store.state["confirmed_accounting_ledger"]) == 1
    assert len(store.state["confirmed_accounting_idempotency"]) == 1


def test_sqlite_accounting_reloads_and_retries_after_stale_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_BACKEND", "sqlite")
    monkeypatch.setattr(config, "DATA_FILE", str(tmp_path / "legacy.json"))
    monkeypatch.setattr(config, "SQLITE_DB_FILE", str(tmp_path / "state.sqlite3"))
    first = DataStore()
    stale = DataStore()
    assert first._state_revision == stale._state_revision

    first_accounting = _confirmed_accounting(event_timestamp=1_101.0)
    second_accounting = _confirmed_accounting(event_timestamp=1_102.0)
    assert first.record_confirmed_accounting(
        "fixture-instance-one", first_accounting,
        value_confirmed_sol_cashflow_usd(first_accounting),
    )
    assert stale.record_confirmed_accounting(
        "fixture-instance-two", second_accounting,
        value_confirmed_sol_cashflow_usd(second_accounting),
    )

    restarted = DataStore()
    ledger = restarted.state["confirmed_accounting_ledger"]
    assert {record["signature"] for record in ledger} == {
        "fixture-instance-one", "fixture-instance-two",
    }
    assert len(restarted.state["confirmed_accounting_idempotency"]) == 2


def test_sqlite_confirmed_accounting_concurrent_process_writes_are_not_lost(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_BACKEND", "sqlite")
    monkeypatch.setattr(config, "DATA_FILE", str(tmp_path / "legacy.json"))
    monkeypatch.setattr(config, "SQLITE_DB_FILE", str(tmp_path / "state.sqlite3"))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "true")
    DataStore()  # Initialize the fixture database before spawning writers.

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    shared_path = str(tmp_path / "state.sqlite3")
    legacy_path = str(tmp_path / "legacy.json")
    workers = [
        context.Process(
            target=_record_accounting_in_process,
            args=(
                shared_path, legacy_path, barrier, results,
                f"fixture-process-{index}", 1_200.0 + index,
            ),
        )
        for index in range(2)
    ]
    for worker in workers:
        worker.start()

    outcomes = [results.get(timeout=30) for _ in workers]
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0
    assert outcomes == [("ok", True), ("ok", True)]

    restarted = DataStore()
    ledger = restarted.state["confirmed_accounting_ledger"]
    assert {record["signature"] for record in ledger} == {
        "fixture-process-0", "fixture-process-1",
    }
    assert len(restarted.state["confirmed_accounting_idempotency"]) == 2


def test_sqlite_same_confirmed_accounting_is_idempotent_across_processes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_BACKEND", "sqlite")
    monkeypatch.setattr(config, "DATA_FILE", str(tmp_path / "legacy.json"))
    monkeypatch.setattr(config, "SQLITE_DB_FILE", str(tmp_path / "state.sqlite3"))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "true")
    DataStore()

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    shared_path = str(tmp_path / "state.sqlite3")
    legacy_path = str(tmp_path / "legacy.json")
    workers = [
        context.Process(
            target=_record_accounting_in_process,
            args=(shared_path, legacy_path, barrier, results, "fixture-shared-signature", 1_300.0),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()

    outcomes = [results.get(timeout=30) for _ in workers]
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0
    assert sorted(inserted for _, inserted in outcomes) == [False, True]

    restarted = DataStore()
    ledger = restarted.state["confirmed_accounting_ledger"]
    assert [record["signature"] for record in ledger] == ["fixture-shared-signature"]
    assert len(restarted.state["confirmed_accounting_idempotency"]) == 1


def test_confirmed_accounting_reconciliation_rolls_back_ledger_and_index_on_save_error(monkeypatch, tmp_path):
    store = DataStore(path=str(tmp_path / "state.json"))
    accounting = _confirmed_accounting()
    unknown = value_confirmed_sol_cashflow_usd(accounting)
    assert store.record_confirmed_accounting("reconcile-fixture-signature", accounting, unknown)
    before_ledger = [dict(row) for row in store.state["confirmed_accounting_ledger"]]
    before_index = dict(store.state["confirmed_accounting_idempotency"])
    # This stub represents a future trusted valuation adapter solely to test
    # the persistence transition. Production currently has no such adapter.
    valued = dict(_value(accounting))
    valued.update({
        "valuation_status": "valued",
        "valuation_reason": None,
        "sol_cashflow_usd": -101.0,
        "fee_usd": 1.0,
    })
    monkeypatch.setattr(
        transaction_accounting,
        "value_confirmed_sol_cashflow_usd",
        lambda *_args, **_kwargs: valued,
    )

    def fail_save():
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "save", fail_save)
    with pytest.raises(OSError, match="disk unavailable"):
        store.record_confirmed_accounting("reconcile-fixture-signature", accounting, valued)

    assert store.state["confirmed_accounting_ledger"] == before_ledger
    assert store.state["confirmed_accounting_idempotency"] == before_index


def test_unknown_to_valued_state_transition_is_supported_only_by_validator_output(monkeypatch, tmp_path):
    store = DataStore(path=str(tmp_path / "state.json"))
    accounting = _confirmed_accounting()
    assert store.record_confirmed_accounting(
        "reconcile-success-signature", accounting, value_confirmed_sol_cashflow_usd(accounting)
    )
    # This only exercises the ledger state machine. It is not provider-origin
    # evidence; the production validator remains fail-closed for caller claims.
    valued = dict(_value(accounting))
    valued.update({
        "valuation_status": "valued",
        "valuation_reason": None,
        "sol_cashflow_usd": -101.0,
        "fee_usd": 1.0,
    })
    monkeypatch.setattr(
        transaction_accounting,
        "value_confirmed_sol_cashflow_usd",
        lambda *_args, **_kwargs: valued,
    )

    assert store.record_confirmed_accounting("reconcile-success-signature", accounting, valued)
    assert store.state["confirmed_accounting_ledger"][0]["valuation_status"] == "valued"
    assert store.state["confirmed_accounting_ledger"][0]["applied_to_paper_pnl"] is False


def test_confirmed_accounting_commit_survives_process_exit_and_restart(tmp_path):
    state_path = tmp_path / "crash-state.json"
    script = r"""
import os
import sys
from monitoring_list import DataStore
from transaction_accounting import value_confirmed_sol_cashflow_usd

accounting = {
    "accounting_schema_version": 1,
    "accounting_source": "confirmed_rpc_transaction_meta",
    "native_delta_lamports": -1_010_000_000,
    "fee_lamports": 10_000_000,
    "event_timestamp": 1_010.0,
}
store = DataStore(path=sys.argv[1])
store.record_confirmed_accounting("crash-fixture-signature", accounting,
                                  value_confirmed_sol_cashflow_usd(accounting))
os._exit(37)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(state_path)],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 37, completed.stderr

    restarted = DataStore(path=str(state_path))
    accounting = _confirmed_accounting()
    assert restarted.record_confirmed_accounting(
        "crash-fixture-signature", accounting, value_confirmed_sol_cashflow_usd(accounting)
    ) is False


def test_confirmed_accounting_idempotency_survives_sqlite_datastore_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_BACKEND", "sqlite")
    monkeypatch.setattr(config, "DATA_FILE", str(tmp_path / "legacy.json"))
    monkeypatch.setattr(config, "SQLITE_DB_FILE", str(tmp_path / "state.sqlite3"))
    accounting = _confirmed_accounting()

    store = DataStore()
    assert store.record_confirmed_accounting(
        "sqlite-fixture-signature", accounting, value_confirmed_sol_cashflow_usd(accounting)
    )

    restarted = DataStore()
    assert restarted.record_confirmed_accounting(
        "sqlite-fixture-signature", accounting, value_confirmed_sol_cashflow_usd(accounting)
    ) is False
    assert len(restarted.state["confirmed_accounting_ledger"]) == 1
    assert len(restarted.state["confirmed_accounting_idempotency"]) == 1

