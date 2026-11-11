import asyncio
import os

import pytest

import config
import live_trader


class FakeKeypair:
    def pubkey(self):
        return "WalletPubkey"


class FakeStore:
    def __init__(self, state):
        self.state = state
        self.saved = False

    def save(self):
        self.saved = True


def _trader(state):
    trader = live_trader.LiveTrader.__new__(live_trader.LiveTrader)
    trader.data_store = FakeStore(state)
    trader.notifier = None
    return trader


def test_emergency_stop_blocks_live_intent_before_submission(monkeypatch):
    monkeypatch.setenv("BOT_EMERGENCY_STOP", "1")

    async def rpc_balance(*args, **kwargs):
        return {"value": 5_000_000_000}

    monkeypatch.setattr(live_trader.rpc_client, "rpc_post", rpc_balance)
    trader = _trader({"open_positions": [], "daily_pnl_sol": 0.0})
    with pytest.raises(ValueError, match="Emergency stop"):
        asyncio.run(trader._reserve_live_buy_intent(
            token_mint="TokenMint",
            entry_price=1.0,
            amount_sol=0.1,
            settings={
                "max_position_sol": 0.5,
                "max_daily_loss_sol": 1.0,
                "max_open_positions": 3,
                "min_balance_reserve_sol": 0.05,
            },
            keypair=FakeKeypair(),
        ))
    assert trader.data_store.state.get("safety_order_intents") is None
    assert trader.data_store.saved is False


def test_unverifiable_balance_blocks_live_intent(monkeypatch):
    monkeypatch.setenv("BOT_EMERGENCY_STOP", "0")

    async def malformed_balance(*args, **kwargs):
        return {"unexpected": "shape"}

    monkeypatch.setattr(live_trader.rpc_client, "rpc_post", malformed_balance)
    trader = _trader({"open_positions": [], "daily_pnl_sol": 0.0})
    with pytest.raises(ValueError, match="Unable to verify"):
        asyncio.run(trader._reserve_live_buy_intent(
            token_mint="TokenMint",
            entry_price=1.0,
            amount_sol=0.1,
            settings={"max_position_sol": 0.5},
            keypair=FakeKeypair(),
        ))
    assert trader.data_store.state.get("safety_order_intents") is None
    assert trader.data_store.saved is False


def test_token_decimals_rejects_missing_rpc_field(monkeypatch):
    async def malformed(*args, **kwargs):
        return {"value": {}}

    monkeypatch.setattr(live_trader.rpc_client, "rpc_post", malformed)
    with pytest.raises(ValueError, match="Impossible de vérifier"):
        asyncio.run(live_trader._get_token_decimals("TokenMint"))


def test_token_decimals_accepts_verified_integer(monkeypatch):
    async def valid(*args, **kwargs):
        return {"value": {"decimals": 9}}

    monkeypatch.setattr(live_trader.rpc_client, "rpc_post", valid)
    assert asyncio.run(live_trader._get_token_decimals("TokenMint")) == 9


def test_token_decimals_rejects_boolean_or_out_of_range(monkeypatch):
    async def invalid(*args, **kwargs):
        return {"value": {"decimals": True}}

    monkeypatch.setattr(live_trader.rpc_client, "rpc_post", invalid)
    with pytest.raises(ValueError, match="Décimales SPL invalides"):
        asyncio.run(live_trader._get_token_decimals("TokenMint"))


def test_ambiguous_sell_timeout_blocks_repeated_sell(monkeypatch):
    async def decimals(*args, **kwargs):
        return 6

    async def execute(*args, **kwargs):
        raise live_trader.jupiter_executor.SwapOutcomeUnknown("signature timeout")

    monkeypatch.setattr(live_trader, "_get_token_decimals", decimals)
    monkeypatch.setattr(live_trader.jupiter_executor, "execute_swap", execute)
    trader = _trader({"open_positions": []})
    trader._get_execution_keypair = lambda: FakeKeypair()

    first = asyncio.run(trader._execute_sell("TokenMint", 100.0, 1.0, {}))
    assert first is None
    assert trader.data_store.state["safety_unresolved_sell_tokens"]["TokenMint"]["status"] == "unknown"
    assert trader.data_store.saved is True

    async def must_not_execute(*args, **kwargs):
        raise AssertionError("an unresolved sell must never be submitted again")

    monkeypatch.setattr(live_trader.jupiter_executor, "execute_swap", must_not_execute)
    second = asyncio.run(trader._execute_sell("TokenMint", 100.0, 1.0, {}))
    assert second is None


def test_confirmed_buy_is_preserved_when_token_metadata_fails(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("BOT_EMERGENCY_STOP", "1")
    intent = SimpleNamespace(idempotency_key="buy:test-confirmed")
    state = {"safety_order_intents": {intent.idempotency_key: {"status": "pending"}}}
    trader = _trader(state)
    trader._get_execution_keypair = lambda: FakeKeypair()

    async def reserve(**kwargs):
        return intent

    async def execute(*args, **kwargs):
        return {
            "signature": "confirmed-signature",
            "input_amount": 100_000_000,
            "output_amount": 250_000,
            "price_impact_pct": 0.5,
        }

    async def decimals(*args, **kwargs):
        raise RuntimeError("RPC metadata unavailable")

    monkeypatch.setattr(trader, "_reserve_live_buy_intent", reserve)
    monkeypatch.setattr(live_trader.jupiter_executor, "execute_swap", execute)
    monkeypatch.setattr(live_trader, "_get_token_decimals", decimals)

    result = asyncio.run(trader._execute_buy("TokenMint", 1.0, {"buy_amount_sol": 0.1}))
    assert result is None
    assert state["safety_order_intents"][intent.idempotency_key]["status"] == "confirmed"
    recovery = state["safety_unaccounted_confirmed_buys"]["confirmed-signature"]
    assert recovery["status"] == "needs_accounting_reconciliation"
    assert recovery["output_amount_raw"] == 250_000
    assert recovery["input_amount_raw"] == 100_000_000
    assert trader.data_store.saved is True


def test_unaccounted_confirmed_buy_blocks_another_buy_for_same_token(monkeypatch):
    trader = _trader({
        "safety_unaccounted_confirmed_buys": {
            "sig-1": {"token_mint": "TokenMint", "status": "needs_accounting_reconciliation"}
        }
    })
    with pytest.raises(ValueError, match="accounting reconciliation"):
        asyncio.run(trader._reserve_live_buy_intent(
            token_mint="TokenMint",
            entry_price=1.0,
            amount_sol=0.1,
            settings={},
            keypair=FakeKeypair(),
        ))
