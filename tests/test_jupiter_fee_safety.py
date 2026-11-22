import asyncio

import pytest

import config
import rpc_client
from jupiter_executor import SwapError, _assert_fee_within_limit, _strict_rpc_fee_lamports
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.transaction import VersionedTransaction
import solders.transaction as solders_transaction
from wallet_manager import WalletManager


def _empty_transaction():
    payer = Keypair()
    message = MessageV0.try_compile(payer.pubkey(), [], [], Hash.default())
    return VersionedTransaction(message, [payer])


@pytest.mark.parametrize("value", [True, 10.0, "10", None, -1])
def test_rpc_fee_parser_rejects_boolean_float_string_null_and_negative(value):
    with pytest.raises(SwapError):
        _strict_rpc_fee_lamports({"value": value})


def test_rpc_fee_parser_accepts_only_nonnegative_integer_lamports():
    assert _strict_rpc_fee_lamports({"value": 10}) == 10


@pytest.mark.parametrize("value", [True, 10.0, None, -1])
def test_swap_fee_limit_fails_closed_on_malformed_rpc_values(monkeypatch, value):
    async def fake_rpc(*args, **kwargs):
        return {"value": value}

    monkeypatch.setattr(rpc_client, "rpc_post", fake_rpc)
    monkeypatch.setattr(config, "MAX_TRANSACTION_FEE_LAMPORTS", 100)
    with pytest.raises(SwapError, match="impossibles à vérifier"):
        asyncio.run(_assert_fee_within_limit(_empty_transaction()))


def test_swap_fee_limit_accepts_within_cap_integer(monkeypatch):
    async def fake_rpc(*args, **kwargs):
        return {"value": 100}

    monkeypatch.setattr(rpc_client, "rpc_post", fake_rpc)
    monkeypatch.setattr(config, "MAX_TRANSACTION_FEE_LAMPORTS", 100)
    asyncio.run(_assert_fee_within_limit(_empty_transaction()))


class _TransferStore:
    def __init__(self):
        self.state = {"emergency_stop": False, "pending_transactions": []}

    def record_pending_transaction(self, record):
        raise AssertionError("the malformed fee must stop before transaction persistence")


class _StopBeforeTransactionConstruction(Exception):
    pass


class _StopBeforeSigning:
    def __new__(cls, *args, **kwargs):
        raise _StopBeforeTransactionConstruction


def _manual_transfer_with_fee(monkeypatch, fee_value):
    monkeypatch.setattr(config, "TRADING_MODE", "LIVE")
    monkeypatch.setattr(config, "LIVE_EXECUTION_ENABLED", True)
    monkeypatch.setattr(config, "MAX_MANUAL_TRANSFER_SOL", 0.05)
    monkeypatch.setattr(config, "MAX_TRANSACTION_FEE_LAMPORTS", 500_000)
    manager = WalletManager(_TransferStore())
    manager.get_keypair = lambda _label: Keypair()
    rpc_methods = []

    async def fake_rpc(payload, **kwargs):
        method = payload.get("method")
        rpc_methods.append(method)
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": str(Hash.default())}}
        if method == "isBlockhashValid":
            return {"value": True}
        if method == "getFeeForMessage":
            return {"value": fee_value}
        raise AssertionError(f"unexpected RPC method before the test stop: {method}")

    monkeypatch.setattr(rpc_client, "rpc_post", fake_rpc)
    return manager, str(Keypair().pubkey()), rpc_methods


@pytest.mark.parametrize("fee_value", [True, 10.5, -1])
def test_manual_transfer_rejects_malformed_fee_before_signing_or_sending(monkeypatch, fee_value):
    manager, destination, rpc_methods = _manual_transfer_with_fee(monkeypatch, fee_value)

    with pytest.raises(ValueError, match="Network fee could not be verified"):
        asyncio.run(manager.send_sol("test", destination, 0.001))

    assert rpc_methods == ["getLatestBlockhash", "isBlockhashValid", "getFeeForMessage"]


def test_manual_transfer_accepts_integer_fee_then_stops_before_transaction_creation(monkeypatch):
    manager, destination, rpc_methods = _manual_transfer_with_fee(monkeypatch, 1)
    monkeypatch.setattr(solders_transaction, "VersionedTransaction", _StopBeforeSigning)

    with pytest.raises(_StopBeforeTransactionConstruction):
        asyncio.run(manager.send_sol("test", destination, 0.001))

    assert rpc_methods == ["getLatestBlockhash", "isBlockhashValid", "getFeeForMessage"]
