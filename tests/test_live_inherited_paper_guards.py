import asyncio

import config
import paper_trader


def test_live_buy_the_dip_does_not_mutate_a_position_or_call_price_provider(monkeypatch):
    monkeypatch.setattr(config, "TRADING_MODE", "LIVE")

    async def unexpected_provider_call(*args, **kwargs):
        raise AssertionError("LIVE must not use the PAPER buy-the-dip simulation")

    monkeypatch.setattr(paper_trader, "_get_pair_data", unexpected_provider_call)
    position = {
        "token_mint": "TokenMint", "settings": {"buy_the_dip_levels": [{"drop_pct": 10, "amount_sol": 0.1}]},
        "ath_market_cap": 1000, "dip_levels_hit": [], "units": 10, "cost_basis_usd": 20,
        "total_sol_invested": 0.1,
    }
    before = dict(position)
    asyncio.run(paper_trader.PaperTrader._check_buy_the_dip(object(), position, 1.0))
    assert position == before


def test_live_sell_initials_refuses_state_only_simulation(monkeypatch):
    monkeypatch.setattr(config, "TRADING_MODE", "LIVE")
    position = {
        "token_mint": "TokenMint", "units": 10, "cost_basis_usd": 20,
        "initials_sold": False,
    }
    before = dict(position)
    result = asyncio.run(paper_trader.PaperTrader.sell_initials(object(), position, 3.0))
    assert result is False
    assert position == before


def test_live_emergency_stop_blocks_buys_and_sells_without_submitting(monkeypatch):
    import jupiter_executor
    from live_trader import LiveTrader

    monkeypatch.setattr(config, "TRADING_MODE", "LIVE")

    class Store:
        state = {"emergency_stop": True, "pending_transactions": []}

    async def unexpected_swap(*_args, **_kwargs):
        raise AssertionError("emergency stop must block every LIVE swap")

    monkeypatch.setattr(jupiter_executor, "execute_swap", unexpected_swap)
    trader = LiveTrader(Store())

    buy_result = asyncio.run(trader._execute_swap_tracked(
        config.SOL_MINT, "TokenMint", 1, 100, None, False,
    ))
    sell_result = asyncio.run(trader._execute_swap_tracked(
        "TokenMint", config.SOL_MINT, 1, 100, None, False,
    ))

    assert buy_result is None
    assert sell_result is None


def test_live_ambiguous_transport_error_persists_stop_without_leaking_error(monkeypatch):
    import jupiter_executor
    import pytest
    from live_trader import LiveTrader

    class Store:
        def __init__(self):
            self.state = {"emergency_stop": False, "pending_transactions": []}

        def record_pending_transaction(self, record):
            self.state["pending_transactions"].append(dict(record))

        def activate_emergency_stop(self, reason):
            self.state["emergency_stop"] = True
            self.state["emergency_stop_reason"] = reason

    class Notifier:
        messages = []

        async def notify(self, *_args):
            self.messages.append(_args)

    async def ambiguous_swap(*_args, on_signed=None, **_kwargs):
        on_signed({"signature": "sig-pending", "status": "submitted"})
        raise OSError("https://provider.invalid/private-path-placeholder")

    monkeypatch.setattr(jupiter_executor, "execute_swap", ambiguous_swap)
    store = Store()
    notifier = Notifier()
    trader = LiveTrader(store, notifier=notifier)

    with pytest.raises(jupiter_executor.SwapError, match="outcome is uncertain"):
        asyncio.run(trader._execute_swap_tracked("input", "output", 1, 100, object(), False))

    assert store.state["emergency_stop"] is True
    assert store.state["pending_transactions"][0]["signature"] == "sig-pending"
    assert "private-path-placeholder" not in notifier.messages[0][1]
