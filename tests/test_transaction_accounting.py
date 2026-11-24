import pytest

from transaction_accounting import (
    AccountingError,
    account_confirmed_sol_transfer,
    account_confirmed_swap,
    value_confirmed_sol_cashflow_usd,
)
from monitoring_list import DataStore

SOL = "So11111111111111111111111111111111111111112"
TOKEN = "Token1111111111111111111111111111111111111"
WALLET = "Wallet111111111111111111111111111111111111"


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


def test_values_confirmed_sol_cashflow_and_fee_from_same_timestamped_rate():
    result = _value()

    assert result["valuation_status"] == "valued"
    assert result["sol_cashflow_usd"] == pytest.approx(-101.0)
    assert result["fee_usd"] == pytest.approx(1.0)
    assert result["fee_included_in_sol_cashflow"] is True
    assert result["rate_age_seconds"] == pytest.approx(10.0)
    assert result["sol_usd_rate_source"] == "verified_fixture_snapshot"


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
            "accounting_schema_version": 1,
            "valuation_schema_version": 1,
            "accounting_source": "confirmed_rpc_transaction_meta",
            "native_delta_lamports": -1_010_000_000,
            "fee_lamports": 10_000_000,
            "fee_status": "confirmed",
            "fee_currency": "SOL",
            "fee_included_in_sol_cashflow": True,
            "accounting_domain": "CONFIRMED_CHAIN",
            "applied_to_paper_pnl": False,
            "event_timestamp": 1_010.0,
            "valuation_status": "valued",
            "valuation_reason": None,
            "sol_cashflow_usd": -101.0,
            "fee_usd": 1.0,
            "sol_usd_rate": 100.0,
            "sol_usd_rate_timestamp": 1_000.0,
            "sol_usd_rate_source": "verified_fixture_snapshot",
            "sol_usd_rate_verified": True,
            "max_rate_age_s": 60.0,
            "rate_age_seconds": 10.0,
        }
    ]


def test_unknown_valuation_is_persisted_without_usd_amounts_and_can_be_reconciled(tmp_path):
    store = DataStore(path=str(tmp_path / "state.json"))
    accounting = _confirmed_accounting()
    unknown = value_confirmed_sol_cashflow_usd(accounting)

    assert store.record_confirmed_accounting("fixture-signature", accounting, unknown) is True
    assert store.state["confirmed_accounting_ledger"][0]["sol_cashflow_usd"] is None
    assert store.state["confirmed_accounting_ledger"][0]["fee_usd"] is None

    assert store.record_confirmed_accounting("fixture-signature", accounting, _value(accounting)) is True
    assert store.state["confirmed_accounting_ledger"][0]["valuation_status"] == "valued"


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

