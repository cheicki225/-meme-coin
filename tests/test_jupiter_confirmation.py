import asyncio

import pytest

import jupiter_executor
import rpc_client


def test_transaction_accounting_waits_for_finalized_commitment(monkeypatch):
    replies = [
        {"value": [{"err": None, "confirmationStatus": "confirmed"}]},
        {"value": [{"err": None, "confirmationStatus": "finalized"}]},
    ]
    calls = []

    async def rpc_post(*args, **kwargs):
        calls.append(args[0]["method"])
        return replies.pop(0)

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(rpc_client, "rpc_post", rpc_post)
    monkeypatch.setattr(jupiter_executor.asyncio, "sleep", no_wait)
    assert asyncio.run(jupiter_executor._confirm_transaction("sig", timeout_s=4)) is True
    assert calls == ["getSignatureStatuses", "getSignatureStatuses"]


def test_confirmed_but_not_finalized_times_out(monkeypatch):
    async def rpc_post(*args, **kwargs):
        return {"value": [{"err": None, "confirmationStatus": "confirmed"}]}

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(rpc_client, "rpc_post", rpc_post)
    monkeypatch.setattr(jupiter_executor.asyncio, "sleep", no_wait)
    with pytest.raises(jupiter_executor.SwapError, match="non confirmée"):
        asyncio.run(jupiter_executor._confirm_transaction("sig", timeout_s=2))
