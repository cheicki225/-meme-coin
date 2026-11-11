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
