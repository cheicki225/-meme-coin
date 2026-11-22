import asyncio
from types import SimpleNamespace

import pytest
from solders.pubkey import Pubkey

import config
import jupiter_executor


def _trace(program_id, *, outer_index=0, accounts=None):
    keys = [Pubkey.from_string(jupiter_executor.JUPITER_PROGRAM_ID), program_id]
    value = {
        "err": None,
        "innerInstructions": [{
            "index": outer_index,
            "instructions": [{
                "programIdIndex": 1,
                "accounts": [0, 1] if accounts is None else accounts,
                "data": "AA==",
            }],
        }],
    }
    return value, keys


def test_simulated_jupiter_route_accepts_only_explicitly_allowlisted_cpi():
    dex_program = Pubkey.new_unique()
    simulation, keys = _trace(dex_program)

    with pytest.raises(jupiter_executor.SwapError, match="outside the reviewed allowlist"):
        jupiter_executor._validate_simulated_cpi_trace(
            simulation,
            account_keys=keys,
            jupiter_instruction_indexes=[0],
            top_level_instruction_count=1,
            allowed_program_ids=set(),
        )

    assert jupiter_executor._validate_simulated_cpi_trace(
        simulation,
        account_keys=keys,
        jupiter_instruction_indexes=[0],
        top_level_instruction_count=1,
        allowed_program_ids={str(dex_program)},
    ) == [str(dex_program)]


def test_cpi_trace_rejects_bad_accounts_and_unrelated_unapproved_calls():
    dex_program = Pubkey.new_unique()
    simulation, keys = _trace(dex_program, accounts=[999])
    with pytest.raises(jupiter_executor.SwapError, match="account index is out of range"):
        jupiter_executor._validate_simulated_cpi_trace(
            simulation, account_keys=keys, jupiter_instruction_indexes=[0],
            top_level_instruction_count=1, allowed_program_ids={str(dex_program)},
        )

    other_program = Pubkey.new_unique()
    simulation["innerInstructions"][0]["instructions"][0]["accounts"] = [0, 1]
    simulation["innerInstructions"].append({
        "index": 1,
        "instructions": [{"programId": str(other_program), "accounts": [], "data": "AA=="}],
    })
    with pytest.raises(jupiter_executor.SwapError, match="outside the reviewed allowlist"):
        jupiter_executor._validate_simulated_cpi_trace(
            simulation, account_keys=keys, jupiter_instruction_indexes=[0],
            top_level_instruction_count=2, allowed_program_ids={str(dex_program)},
        )


@pytest.mark.parametrize("account", ["unresolved-pubkey", {"pubkey": "unresolved-pubkey"}, None])
def test_cpi_trace_rejects_account_references_without_resolved_indexes(account):
    dex_program = Pubkey.new_unique()
    simulation, keys = _trace(dex_program, accounts=[account])
    with pytest.raises(jupiter_executor.SwapError, match="account map is unresolved"):
        jupiter_executor._validate_simulated_cpi_trace(
            simulation, account_keys=keys, jupiter_instruction_indexes=[0],
            top_level_instruction_count=1, allowed_program_ids={str(dex_program)},
        )


def test_cpi_trace_requires_success_and_a_complete_jupiter_inner_trace():
    failed = {"err": {"InstructionError": [0, "Failed"]}, "innerInstructions": []}
    with pytest.raises(jupiter_executor.SwapError, match="simulation failed"):
        jupiter_executor._validate_simulated_cpi_trace(
            failed, account_keys=[], jupiter_instruction_indexes=[0],
            top_level_instruction_count=1, allowed_program_ids=set(),
        )

    missing = {"err": None, "innerInstructions": []}
    with pytest.raises(jupiter_executor.SwapError, match="trace is missing"):
        jupiter_executor._validate_simulated_cpi_trace(
            missing, account_keys=[], jupiter_instruction_indexes=[0],
            top_level_instruction_count=1, allowed_program_ids=set(),
        )


def test_empty_default_allowlist_refuses_before_rpc_or_signing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JUPITER_CPI_PROGRAM_ALLOWLIST", "", raising=False)
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    calls = []

    async def unexpected_rpc(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("empty allowlist must be checked before RPC")

    monkeypatch.setattr(jupiter_executor.rpc_client, "rpc_post_strict", unexpected_rpc)
    with pytest.raises(jupiter_executor.SwapError, match="No reviewed Jupiter CPI/DEX"):
        asyncio.run(jupiter_executor._simulate_and_validate_jupiter_cpis(object(), ([], [])))
    assert calls == []


def test_simulation_uses_unsigned_transaction_with_cpi_capture(monkeypatch, tmp_path):
    dex_program = Pubkey.new_unique()
    jupiter_program = Pubkey.from_string(jupiter_executor.JUPITER_PROGRAM_ID)
    monkeypatch.setattr(config, "JUPITER_CPI_PROGRAM_ALLOWLIST", str(dex_program), raising=False)
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    captured = []

    class UnsignedTransaction:
        message = SimpleNamespace(
            account_keys=[jupiter_program, dex_program],
            instructions=[SimpleNamespace(program_id_index=0)],
        )

        def __bytes__(self):
            return b"unsigned-transaction-fixture"

    async def fake_rpc(payload, timeout):
        captured.append((payload, timeout))
        return {"value": _trace(dex_program)[0]}

    monkeypatch.setattr(jupiter_executor.rpc_client, "rpc_post_strict", fake_rpc)
    found = asyncio.run(jupiter_executor._simulate_and_validate_jupiter_cpis(
        UnsignedTransaction(), ([], []),
    ))

    assert found == [str(dex_program)]
    payload, timeout = captured[0]
    assert payload["method"] == "simulateTransaction"
    assert payload["params"][1]["sigVerify"] is False
    assert payload["params"][1]["innerInstructions"] is True
    assert timeout == 20
