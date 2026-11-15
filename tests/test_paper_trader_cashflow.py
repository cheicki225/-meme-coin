import asyncio

import paper_trader


def test_sell_uses_confirmed_sol_cashflow_without_repricing(monkeypatch):
    async def unexpected_price_lookup():
        raise AssertionError("confirmed SOL cashflow must not be converted a second time")

    monkeypatch.setattr(paper_trader, "get_sol_usd_rate", unexpected_price_lookup)
    result = asyncio.run(paper_trader._sold_sol_cashflow(
        {"proceeds_usd": 42.0, "sol_cashflow": 0.123456789}, 42.0
    ))
    assert result == 0.123456789


def test_paper_sell_falls_back_to_current_sol_usd_rate(monkeypatch):
    async def sol_rate():
        return 200.0

    monkeypatch.setattr(paper_trader, "get_sol_usd_rate", sol_rate)
    result = asyncio.run(paper_trader._sold_sol_cashflow({"proceeds_usd": 50.0}, 50.0))
    assert result == 0.25
