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
