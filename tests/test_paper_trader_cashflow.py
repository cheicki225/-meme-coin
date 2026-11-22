import asyncio

import paper_trader
import pytest


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


def test_paper_entry_fee_is_in_cost_basis_and_total_sol_invested(monkeypatch):
    async def sol_rate():
        return 200.0

    monkeypatch.setattr(paper_trader, "get_sol_usd_rate", sol_rate)
    result = asyncio.run(paper_trader._paper_entry_accounting(20.0, 0.1, 0.003))

    assert result["cost_basis_usd"] == pytest.approx(20.6)
    assert result["total_sol_invested"] == pytest.approx(0.103)
    assert result["simulated_fees_sol"] == pytest.approx(0.003)
    assert result["simulated_fees_usd"] == pytest.approx(0.6)


def test_paper_sell_simulates_a_fee_and_returns_fee_in_usd(monkeypatch):
    async def sol_rate():
        return 200.0

    monkeypatch.setattr(paper_trader, "get_sol_usd_rate", sol_rate)
    trader = paper_trader.PaperTrader(data_store=None)
    result = asyncio.run(trader._execute_sell("mint", 2.0, 5.0, {"buy_mode": "simple"}))

    assert result["proceeds_usd"] == 10.0
    assert result["simulated_fee_sol"] == paper_trader.SIMPLE_TYPICAL_FEE_SOL
    assert result["simulated_fee_usd"] == pytest.approx(0.6)


def test_live_mode_never_adds_a_paper_fee(monkeypatch):
    monkeypatch.setattr(paper_trader.config, "TRADING_MODE", "LIVE")

    async def unexpected_rate_lookup():
        raise AssertionError("LIVE sell must not estimate a PAPER fee")

    monkeypatch.setattr(paper_trader, "get_sol_usd_rate", unexpected_rate_lookup)
    trader = paper_trader.PaperTrader(data_store=None)
    result = asyncio.run(trader._execute_sell("mint", 2.0, 5.0, {"buy_mode": "hardcore"}))

    assert result["proceeds_usd"] == 10.0
    assert result["simulated_fee_sol"] == 0.0
    assert result["simulated_fee_usd"] == 0.0


def test_partial_close_subtracts_sell_fee_from_realized_pnl(monkeypatch):
    class Store:
        def __init__(self):
            self.state = {
                "open_positions": [], "trades": [], "total_pnl_usd": 0.0,
                "session_losses_usd": 0.0,
            }

        def save(self):
            pass

        def resolve_pending_transaction(self, *_args):
            raise AssertionError("paper close has no transaction signature")

    store = Store()
    trader = paper_trader.PaperTrader(data_store=store)
    position = {
        "token_mint": "mint", "settings": {}, "units": 10.0,
        "cost_basis_usd": 11.0, "tp_levels_hit": [],
        "simulated_fees_sol": 0.003, "simulated_fees_usd": 0.6,
    }

    async def execute_sell(*_args):
        return {
            "proceeds_usd": 6.1, "sol_cashflow": 0.05, "signature": None,
            "simulated_fee_sol": 0.003, "simulated_fee_usd": 0.5,
        }

    monkeypatch.setattr(trader, "_execute_sell", execute_sell)
    asyncio.run(trader._partial_close(position, 1.22, 10.0, 0.5, 0))

    # Half the entry cost (including the buy fee) is allocated to this sale.
    assert store.state["total_pnl_usd"] == pytest.approx(0.1)
    assert store.state["trades"][0]["pnl_usd"] == pytest.approx(0.1)
    assert position["cost_basis_usd"] == pytest.approx(5.5)
    assert position["total_sol_received"] == pytest.approx(0.047)
    assert position["simulated_fees_sol"] == pytest.approx(0.006)
    assert position["simulated_fees_usd"] == pytest.approx(1.1)


def test_buy_the_dip_adds_purchase_and_fee_to_position_basis(monkeypatch):
    class Store:
        def __init__(self):
            self.state = {}

        def save(self):
            pass

    async def pair_data(_mint):
        return {"marketCap": 700}

    async def sol_rate():
        return 200.0

    monkeypatch.setattr(paper_trader, "_get_pair_data", pair_data)
    monkeypatch.setattr(paper_trader, "get_sol_usd_rate", sol_rate)
    position = {
        "token_mint": "mint", "settings": {"buy_mode": "simple", "buy_the_dip_levels": [
            {"drop_pct": 20, "amount_sol": 0.1},
        ]},
        "buy_mode": "simple", "ath_market_cap": 1000,
        "dip_levels_hit": [], "units": 10.0, "cost_basis_usd": 20.0,
        "total_sol_invested": 0.103, "simulated_fees_sol": 0.003,
        "simulated_fees_usd": 0.6,
    }

    asyncio.run(paper_trader.PaperTrader(Store())._check_buy_the_dip(position, 2.0))

    assert position["units"] == pytest.approx(20.0)
    assert position["cost_basis_usd"] == pytest.approx(40.6)
    assert position["total_sol_invested"] == pytest.approx(0.206)
    assert position["simulated_fees_sol"] == pytest.approx(0.006)
    assert position["simulated_fees_usd"] == pytest.approx(1.2)
    assert position["dip_levels_hit"] == [0]


def test_sell_initials_books_fee_in_realized_pnl_and_net_sol_recovery(monkeypatch):
    class Store:
        def __init__(self):
            self.state = {"trades": [], "total_pnl_usd": 0.0, "session_losses_usd": 0.0}

        def save(self):
            pass

    async def sol_rate():
        return 200.0

    monkeypatch.setattr(paper_trader, "get_sol_usd_rate", sol_rate)
    store = Store()
    position = {
        "token_mint": "mint", "settings": {"buy_mode": "simple"},
        "buy_mode": "simple", "units": 10.0, "cost_basis_usd": 11.0,
        "initials_sold": False, "total_sol_invested": 0.103,
        "total_sol_received": 0.0, "simulated_fees_sol": 0.003,
        "simulated_fees_usd": 0.6,
    }

    result = asyncio.run(paper_trader.PaperTrader(store).sell_initials(position, 2.0))

    assert result is True
    assert position["initials_sold"] is True
    assert position["units"] == pytest.approx(4.2)
    assert position["cost_basis_usd"] == 0
    assert position["total_sol_received"] == pytest.approx(0.055)
    assert position["simulated_fees_sol"] == pytest.approx(0.006)
    assert position["simulated_fees_usd"] == pytest.approx(1.2)
    assert store.state["trades"][0]["pnl_usd"] == pytest.approx(0.0)
    assert store.state["total_pnl_usd"] == pytest.approx(0.0)
