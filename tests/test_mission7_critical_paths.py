"""Mission 7 regressions for financial validation and restart safety."""

from __future__ import annotations

import asyncio
import base64
from copy import deepcopy

import pytest
from solders.address_lookup_table_account import ADDRESS_LOOKUP_TABLE_ID, AddressLookupTable
from solders.pubkey import Pubkey

import config
import jupiter_executor
from execution_recovery import inspect_signature_on_chain, reconcile_pending_transactions_readonly
from transaction_accounting import AccountingError, account_confirmed_sol_transfer, account_confirmed_swap


SOL = "So11111111111111111111111111111111111111112"
INPUT_MINT = "InputMint111111111111111111111111111111111"
OUTPUT_MINT = "OutputMint1111111111111111111111111111111"
WALLET = "Wallet111111111111111111111111111111111111"
DESTINATION = "Destination111111111111111111111111111111"


def _successful_cpi_trace():
    dex = Pubkey.new_unique()
    keys = [Pubkey.from_string(jupiter_executor.JUPITER_PROGRAM_ID), dex]
    value = {
        "err": None,
        "innerInstructions": [{
            "index": 0,
            "instructions": [{"programIdIndex": 1, "accounts": [0, 1], "data": "AA=="}],
        }],
    }
    return value, keys, str(dex)


@pytest.mark.parametrize("raw", [None, "x" * 4097, "not-a-program-id"])
def test_jupiter_allowlist_rejects_invalid_configuration_before_signing(monkeypatch, raw):
    monkeypatch.setattr(config, "JUPITER_CPI_PROGRAM_ALLOWLIST", raw, raising=False)
    with pytest.raises(jupiter_executor.SwapError, match="allowlist"):
        jupiter_executor._parse_jupiter_cpi_allowlist()


@pytest.mark.parametrize("mutation,match", [
    (lambda value: value.pop("err"), "result is incomplete"),
    (lambda value: value.update(innerInstructions=None), "trace is unavailable"),
    (lambda value: value.update(innerInstructions=[None]), "malformed instruction group"),
    (lambda value: value["innerInstructions"][0].update(index=4), "invalid outer instruction index"),
    (lambda value: value["innerInstructions"][0].update(instructions=None), "invalid outer instruction index"),
    (lambda value: value["innerInstructions"][0].update(instructions=[None]), "malformed instruction"),
    (lambda value: value["innerInstructions"][0]["instructions"][0].update(accounts=None), "account map is malformed"),
    (lambda value: value["innerInstructions"][0]["instructions"][0].update(accounts=[True]), "account map is unresolved"),
    (lambda value: value["innerInstructions"][0]["instructions"][0].update(programId="invalid"), "program ID is malformed"),
])
def test_jupiter_cpi_trace_rejects_malformed_or_incomplete_responses(mutation, match):
    value, keys, dex_id = _successful_cpi_trace()
    value = deepcopy(value)
    mutation(value)
    with pytest.raises(jupiter_executor.SwapError, match=match):
        jupiter_executor._validate_simulated_cpi_trace(
            value,
            account_keys=keys,
            jupiter_instruction_indexes=[0],
            top_level_instruction_count=1,
            allowed_program_ids={dex_id},
        )


def test_jupiter_cpi_trace_requires_exactly_one_identifiable_swap():
    value, keys, dex_id = _successful_cpi_trace()
    for indexes, count in (([], 1), ([0, 1], 2), ([0], 0)):
        with pytest.raises(jupiter_executor.SwapError, match="cannot identify"):
            jupiter_executor._validate_simulated_cpi_trace(
                value,
                account_keys=keys,
                jupiter_instruction_indexes=indexes,
                top_level_instruction_count=count,
                allowed_program_ids={dex_id},
            )


def test_jupiter_paper_mode_refuses_before_loading_wallet_or_calling_network(monkeypatch):
    monkeypatch.setattr(config, "TRADING_MODE", "PAPER")
    monkeypatch.setattr(config, "LIVE_EXECUTION_ENABLED", False)
    calls = []

    def forbidden_wallet_load():
        calls.append("wallet")
        raise AssertionError("PAPER guard must run before wallet loading")

    async def forbidden_order(*_args, **_kwargs):
        calls.append("order")
        raise AssertionError("PAPER guard must run before network access")

    monkeypatch.setattr(jupiter_executor.wallet, "load_keypair", forbidden_wallet_load)
    monkeypatch.setattr(jupiter_executor, "get_quote", forbidden_order)
    with pytest.raises(jupiter_executor.SwapError, match="LIVE execution is disabled"):
        asyncio.run(jupiter_executor.execute_swap(SOL, OUTPUT_MINT, 1, 1))
    assert calls == []


def test_lookup_table_requires_expected_owner_and_returns_empty_without_lookups(monkeypatch):
    empty = asyncio.run(jupiter_executor._resolve_lookup_addresses(
        type("Message", (), {"address_table_lookups": []})(),
    ))
    assert empty == ([], [])

    lookup = type("Lookup", (), {
        "account_key": Pubkey.new_unique(),
        "writable_indexes": [],
        "readonly_indexes": [],
    })()

    async def wrong_owner(*_args, **_kwargs):
        return {"value": {"owner": "unexpected-program", "data": ["AA==", "base64"]}}

    monkeypatch.setattr(jupiter_executor.rpc_client, "rpc_post", wrong_owner)
    with pytest.raises(jupiter_executor.SwapError, match="unexpected program"):
        asyncio.run(jupiter_executor._resolve_lookup_addresses(
            type("Message", (), {"address_table_lookups": [lookup]})(),
        ))


def test_cpi_simulation_rejects_unresolvable_jupiter_instruction_index(monkeypatch):
    dex = Pubkey.new_unique()
    monkeypatch.setattr(config, "JUPITER_CPI_PROGRAM_ALLOWLIST", str(dex), raising=False)

    class UnsignedTransaction:
        message = type("Message", (), {
            "account_keys": [Pubkey.from_string(jupiter_executor.JUPITER_PROGRAM_ID)],
            "instructions": [type("Instruction", (), {"program_id_index": 4})()],
        })()

        def __bytes__(self):
            return b"unsigned-fixture"

    async def forbidden_rpc(*_args, **_kwargs):
        raise AssertionError("invalid instruction index must fail before RPC")

    monkeypatch.setattr(jupiter_executor.rpc_client, "rpc_post_strict", forbidden_rpc)
    with pytest.raises(jupiter_executor.SwapError, match="program index is invalid"):
        asyncio.run(jupiter_executor._simulate_and_validate_jupiter_cpis(
            UnsignedTransaction(), ([], []),
        ))


def test_blockhash_check_refuses_expired_or_unverifiable_value(monkeypatch):
    async def invalid_hash(*_args, **_kwargs):
        return {"value": False}

    monkeypatch.setattr(jupiter_executor.rpc_client, "rpc_post", invalid_hash)
    with pytest.raises(jupiter_executor.SwapError, match="absent ou expiré"):
        asyncio.run(jupiter_executor._assert_blockhash_valid("fixture-blockhash"))


def test_confirmed_transaction_fetch_retries_only_read_metadata(monkeypatch):
    responses = [None, {"meta": None}, {"meta": {"err": None}}]
    methods = []

    async def fake_rpc(payload, timeout):
        methods.append((payload["method"], timeout))
        return responses.pop(0)

    async def no_wait(_delay):
        return None

    monkeypatch.setattr(jupiter_executor.rpc_client, "rpc_post", fake_rpc)
    monkeypatch.setattr(jupiter_executor.asyncio, "sleep", no_wait)
    result = asyncio.run(jupiter_executor._get_confirmed_transaction("known-signature", attempts=3))
    assert result == {"meta": {"err": None}}
    assert methods == [("getTransaction", 15)] * 3


def test_confirmed_transaction_without_finalized_metadata_stays_blocked(monkeypatch):
    async def no_metadata(_payload, timeout):
        assert timeout == 15
        return None

    monkeypatch.setattr(jupiter_executor.rpc_client, "rpc_post", no_metadata)
    with pytest.raises(jupiter_executor.SwapError, match="pending signature is retained"):
        asyncio.run(jupiter_executor._get_confirmed_transaction("unknown-signature", attempts=1))


@pytest.mark.parametrize("signature", [None, "", "   ", "x" * 101])
def test_signature_inspection_rejects_invalid_input_without_rpc(signature):
    async def forbidden_rpc(*_args, **_kwargs):
        raise AssertionError("invalid signature must not trigger RPC")

    result = asyncio.run(inspect_signature_on_chain(signature, rpc_post=forbidden_rpc))
    assert result == {"status": "invalid_signature"}


@pytest.mark.parametrize("failure_stage", ["status", "transaction"])
def test_signature_inspection_rpc_errors_remain_unknown(failure_stage):
    calls = 0

    async def failing_rpc(payload, timeout):
        nonlocal calls
        calls += 1
        if failure_stage == "status" or calls == 2:
            raise TimeoutError("isolated fixture timeout")
        return {"value": [{"confirmationStatus": "finalized", "err": None}]}

    result = asyncio.run(inspect_signature_on_chain("sig", rpc_post=failing_rpc))
    assert result == {"status": "unknown", "reason": "TimeoutError"}


@pytest.mark.parametrize("meta", [None, {}, {"fee": True}, {"fee": -1}, {"fee": "1"}])
def test_signature_inspection_requires_finalized_transaction_fee(meta):
    async def fake_rpc(payload, timeout):
        if payload["method"] == "getSignatureStatuses":
            return {"value": [{"confirmationStatus": "finalized", "err": None}]}
        return {"meta": meta} if meta is not None else None

    result = asyncio.run(inspect_signature_on_chain("sig", rpc_post=fake_rpc))
    assert result["status"] == "unknown"


def test_reconciliation_with_zero_budget_keeps_all_signatures_pending():
    store = type("Store", (), {})()
    store.state = {"pending_transactions": [{"signature": "still-unknown"}]}

    async def forbidden_inspector(_signature):
        raise AssertionError("zero reconciliation budget must not query RPC")

    result = asyncio.run(reconcile_pending_transactions_readonly(
        store, inspector=forbidden_inspector, max_checks=0,
    ))
    assert result["checked"] == 0
    assert result["deferred"] == 1
    assert result["remaining"] == 1
    assert store.state["pending_transactions"][0]["signature"] == "still-unknown"


def _token_to_token_transaction():
    return {
        "transaction": {"message": {"accountKeys": [{"pubkey": WALLET}, "input-ata", "output-ata"]}},
        "meta": {
            "err": None,
            "fee": 5_000,
            "preBalances": [1_000_000, 0, 0],
            "postBalances": [1_000_000, 0, 0],
            "preTokenBalances": [
                {"owner": WALLET, "mint": INPUT_MINT, "uiTokenAmount": {"amount": "12", "decimals": 0}},
                {"owner": WALLET, "mint": INPUT_MINT, "uiTokenAmount": {"amount": "8", "decimals": 0}},
                {"owner": "other-wallet", "mint": "ignored", "uiTokenAmount": {"amount": "bad", "decimals": "bad"}},
            ],
            "postTokenBalances": [
                {"owner": WALLET, "mint": OUTPUT_MINT, "uiTokenAmount": {"amount": "9", "decimals": 0}},
                {"owner": WALLET, "mint": OUTPUT_MINT, "uiTokenAmount": {"amount": "7", "decimals": 0}},
            ],
        },
    }


def test_token_to_token_accounting_aggregates_wallet_accounts_and_ignores_other_owner():
    result = account_confirmed_swap(WALLET, INPUT_MINT, OUTPUT_MINT, _token_to_token_transaction())
    assert result["input_delta_raw"] == -20
    assert result["output_delta_raw"] == 16
    assert result["native_delta_lamports"] == 0
    assert result["fee_lamports"] == 5_000


def test_accounting_rejects_inconsistent_token_precision_between_snapshots():
    transaction = _token_to_token_transaction()
    transaction["meta"]["postTokenBalances"][0]["uiTokenAmount"]["decimals"] = 9
    with pytest.raises(AccountingError, match="Inconsistent decimals"):
        account_confirmed_swap(WALLET, INPUT_MINT, OUTPUT_MINT, transaction)


@pytest.mark.parametrize("change,match", [
    (lambda tx: tx["meta"].update(preBalances=[]), "incomplete"),
    (lambda tx: tx["meta"].update(fee=None), "malformed"),
    (lambda tx: tx["meta"].update(err={"failed": True}), "failed on chain"),
    (lambda tx: tx["transaction"]["message"].update(accountKeys=["another-wallet"]), "missing from transaction"),
])
def test_swap_accounting_rejects_incomplete_or_inconsistent_wallet_metadata(change, match):
    transaction = _token_to_token_transaction()
    change(transaction)
    with pytest.raises(AccountingError, match=match):
        account_confirmed_swap(WALLET, INPUT_MINT, OUTPUT_MINT, transaction)


def test_sol_transfer_accounting_rejects_wrong_recipient_and_amount_types():
    transaction = {
        "transaction": {"message": {"accountKeys": [WALLET, DESTINATION]}},
        "meta": {"err": None, "fee": 5_000, "preBalances": [2_000_000, 0], "postBalances": [995_000, 1_000_000]},
    }
    for amount in (True, 1.0, 0, -1):
        with pytest.raises(AccountingError, match="positive integer"):
            account_confirmed_sol_transfer(WALLET, DESTINATION, amount, transaction)
    with pytest.raises(AccountingError, match="destination"):
        account_confirmed_sol_transfer(WALLET, "wrong-destination", 1_000_000, transaction)


def test_sol_transfer_accounting_rejects_token_side_effects():
    transaction = {
        "transaction": {"message": {"accountKeys": [WALLET, DESTINATION]}},
        "meta": {
            "err": None,
            "fee": 5_000,
            "preBalances": [2_000_000, 0],
            "postBalances": [995_000, 1_000_000],
            "preTokenBalances": [],
            "postTokenBalances": [{
                "owner": WALLET, "mint": INPUT_MINT,
                "uiTokenAmount": {"amount": "1", "decimals": 0},
            }],
        },
    }
    with pytest.raises(AccountingError, match="also changed wallet token balances"):
        account_confirmed_sol_transfer(WALLET, DESTINATION, 1_000_000, transaction)
def _lookup_message_with_indexes(writable=(1,), readonly=(0,)):
    lookup = type(
        "Lookup",
        (),
        {
            "account_key": Pubkey.new_unique(),
            "writable_indexes": list(writable),
            "readonly_indexes": list(readonly),
        },
    )()
    return type("Message", (), {"address_table_lookups": [lookup]})()


def test_lookup_resolution_collects_writable_and_readonly_addresses(monkeypatch):
    readonly_address, writable_address = Pubkey.new_unique(), Pubkey.new_unique()
    decoded_table = type(
        "DecodedTable", (), {"addresses": [readonly_address, writable_address]}
    )()
    monkeypatch.setattr(
        AddressLookupTable, "deserialize", staticmethod(lambda _data: decoded_table)
    )
    encoded_table = base64.b64encode(b"isolated-address-table").decode("ascii")

    async def read_address_table(payload, timeout):
        assert payload["method"] == "getAccountInfo"
        assert timeout == 10
        return {
            "value": {
                "owner": str(ADDRESS_LOOKUP_TABLE_ID),
                "data": [encoded_table, "base64"],
            }
        }

    monkeypatch.setattr(jupiter_executor.rpc_client, "rpc_post", read_address_table)
    writable, readonly = asyncio.run(
        jupiter_executor._resolve_lookup_addresses(_lookup_message_with_indexes())
    )
    assert writable == [writable_address]
    assert readonly == [readonly_address]


@pytest.mark.parametrize(
    "data_value,error",
    [
        ([], "encoding is invalid"),
        (["AA==", "base58"], "encoding is invalid"),
        (None, "data is unavailable"),
        ("!!!", "could not be decoded safely"),
    ],
)
def test_lookup_resolution_rejects_bad_encoding_or_data(monkeypatch, data_value, error):
    monkeypatch.setattr(
        AddressLookupTable,
        "deserialize",
        staticmethod(lambda _data: type("DecodedTable", (), {"addresses": []})()),
    )

    async def read_address_table(_payload, **_kwargs):
        return {
            "value": {
                "owner": str(ADDRESS_LOOKUP_TABLE_ID),
                "data": data_value,
            }
        }

    monkeypatch.setattr(jupiter_executor.rpc_client, "rpc_post", read_address_table)
    with pytest.raises(jupiter_executor.SwapError, match=error):
        asyncio.run(
            jupiter_executor._resolve_lookup_addresses(
                _lookup_message_with_indexes(writable=(), readonly=())
            )
        )


def test_lookup_resolution_rejects_out_of_range_address_indexes(monkeypatch):
    decoded_table = type("DecodedTable", (), {"addresses": [Pubkey.new_unique()]})()
    monkeypatch.setattr(
        AddressLookupTable, "deserialize", staticmethod(lambda _data: decoded_table)
    )

    async def read_address_table(_payload, **_kwargs):
        return {
            "value": {
                "owner": str(ADDRESS_LOOKUP_TABLE_ID),
                "data": base64.b64encode(b"valid-fixture").decode("ascii"),
            }
        }

    monkeypatch.setattr(jupiter_executor.rpc_client, "rpc_post", read_address_table)
    with pytest.raises(jupiter_executor.SwapError, match="writable index is out of range"):
        asyncio.run(
            jupiter_executor._resolve_lookup_addresses(
                _lookup_message_with_indexes(writable=(1,), readonly=())
            )
        )


def test_jupiter_quote_requires_mapping_before_field_access():
    with pytest.raises(jupiter_executor.SwapError, match="objet attendu"):
        jupiter_executor._validate_quote(None, SOL, OUTPUT_MINT, 1_000, 100)
