import asyncio
import time

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


def test_paper_entry_fee_uses_the_rate_snapshot_from_the_buy(monkeypatch):
    rates = iter([200.0, 100.0])
    rate_calls = []

    async def sol_rate():
        rate_calls.append(True)
        return next(rates)

    monkeypatch.setattr(paper_trader, "get_sol_usd_rate", sol_rate)
    trader = paper_trader.PaperTrader(data_store=None)
    buy = asyncio.run(trader._execute_buy("mint", 2.0, {"buy_amount_sol": 0.1}))
    accounting = asyncio.run(paper_trader._paper_entry_accounting(
        buy["cost_basis_usd"], 0.1, 0.003, sol_usd_rate=buy["sol_usd_rate"]
    ))

    assert buy["cost_basis_usd"] == pytest.approx(20.0)
    assert accounting["cost_basis_usd"] == pytest.approx(20.6)
    assert accounting["simulated_fees_usd"] == pytest.approx(0.6)
    assert rate_calls == [True]


def test_paper_sell_simulates_a_fee_and_returns_fee_in_usd(monkeypatch):
    rates = iter([200.0, 100.0])
    calls = []

    async def sol_rate():
        calls.append(True)
        return next(rates)

    monkeypatch.setattr(paper_trader, "get_sol_usd_rate", sol_rate)
    trader = paper_trader.PaperTrader(data_store=None)
    result = asyncio.run(trader._execute_sell("mint", 2.0, 5.0, {"buy_mode": "simple"}))

    assert result["proceeds_usd"] == 10.0
    assert result["simulated_fee_sol"] == paper_trader.SIMPLE_TYPICAL_FEE_SOL
    assert result["simulated_fee_usd"] == pytest.approx(0.6)
    cashflow = asyncio.run(paper_trader._sold_sol_cashflow(result, result["proceeds_usd"]))
    assert cashflow == pytest.approx(0.05)
    assert len(calls) == 1


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
    assert position["fee_accounting_complete"] is False
    assert store.state["trades"][0]["fee_accounting_complete"] is False
    assert store.state["trades"][0]["fee_accounting_note"] == "legacy_fee_history_unavailable"


def test_buy_the_dip_adds_purchase_and_fee_to_position_basis(monkeypatch):
    class Store:
        def __init__(self, position):
            self.state = {"open_positions": [position]}
            self.save_calls = 0

        def save(self):
            self.save_calls += 1

    async def pair_data(_mint):
        return {"marketCap": 700}

    rate_calls = []

    async def sol_rate():
        rate_calls.append(True)
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
    store = Store(position)
    trader = paper_trader.PaperTrader(store)
    monkeypatch.setattr(paper_trader.config, "MAX_OPEN_POSITIONS", 1)

    asyncio.run(trader._check_buy_the_dip(position, 2.0))

    assert position["units"] == pytest.approx(20.0)
    assert position["cost_basis_usd"] == pytest.approx(40.6)
    assert position["total_sol_invested"] == pytest.approx(0.206)
    assert position["simulated_fees_sol"] == pytest.approx(0.006)
    assert position["simulated_fees_usd"] == pytest.approx(1.2)
    assert position["dip_levels_hit"] == [0]
    assert position["fee_accounting_complete"] is False
    assert "fee_accounting_version" not in position
    assert store.save_calls == 1
    assert trader._risk_reservations == {}
    assert rate_calls == [True]


@pytest.mark.parametrize(
    ("state_updates", "limit_updates"),
    [
        ({"emergency_stop": True}, {}),
        ({}, {"MAX_OPEN_EXPOSURE_USD": 40.0}),
    ],
)
def test_buy_the_dip_refuses_active_stop_and_exposure_cap_without_mutation(
    monkeypatch, state_updates, limit_updates
):
    class Store:
        def __init__(self, position):
            self.state = {"open_positions": [position], **state_updates}
            self.save_calls = 0

        def save(self):
            self.save_calls += 1

    async def pair_data(_mint):
        return {"marketCap": 700}

    async def sol_rate():
        return 200.0

    monkeypatch.setattr(paper_trader, "_get_pair_data", pair_data)
    monkeypatch.setattr(paper_trader, "get_sol_usd_rate", sol_rate)
    for name, value in limit_updates.items():
        monkeypatch.setattr(paper_trader.config, name, value)

    position = {
        "token_mint": "mint", "settings": {"buy_mode": "simple", "buy_the_dip_levels": [
            {"drop_pct": 20, "amount_sol": 0.1},
        ]},
        "buy_mode": "simple", "ath_market_cap": 1000,
        "dip_levels_hit": [], "units": 10.0, "cost_basis_usd": 20.0,
        "total_sol_invested": 0.103, "simulated_fees_sol": 0.003,
        "simulated_fees_usd": 0.6,
    }
    store = Store(position)
    trader = paper_trader.PaperTrader(store)

    asyncio.run(trader._check_buy_the_dip(position, 2.0))

    assert position["units"] == pytest.approx(10.0)
    assert position["cost_basis_usd"] == pytest.approx(20.0)
    assert position["total_sol_invested"] == pytest.approx(0.103)
    assert position["simulated_fees_sol"] == pytest.approx(0.003)
    assert position["simulated_fees_usd"] == pytest.approx(0.6)
    assert position["dip_levels_hit"] == []
    assert store.save_calls == 0
    assert trader._risk_reservations == {}


def test_buy_the_dip_refuses_malformed_position_exposure_without_mutation(monkeypatch):
    class Store:
        def __init__(self, position):
            self.state = {"open_positions": [position]}
            self.save_calls = 0

        def save(self):
            self.save_calls += 1

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
        "dip_levels_hit": [], "units": 10.0, "cost_basis_usd": float("nan"),
        "total_sol_invested": 0.103, "simulated_fees_sol": 0.003,
        "simulated_fees_usd": 0.6,
    }
    store = Store(position)
    trader = paper_trader.PaperTrader(store)

    asyncio.run(trader._check_buy_the_dip(position, 2.0))

    assert position["units"] == pytest.approx(10.0)
    assert position["cost_basis_usd"] != position["cost_basis_usd"]
    assert position["dip_levels_hit"] == []
    assert store.save_calls == 0
    assert trader._risk_reservations == {}


def test_buy_the_dip_save_failure_rolls_back_position_and_releases_reservation(monkeypatch):
    class Store:
        def __init__(self, position):
            self.state = {"open_positions": [position]}

        def save(self):
            raise OSError("simulated state persistence failure")

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
    store = Store(position)
    trader = paper_trader.PaperTrader(store)

    with pytest.raises(OSError, match="simulated state persistence failure"):
        asyncio.run(trader._check_buy_the_dip(position, 2.0))

    assert position["units"] == pytest.approx(10.0)
    assert position["cost_basis_usd"] == pytest.approx(20.0)
    assert position["total_sol_invested"] == pytest.approx(0.103)
    assert position["simulated_fees_sol"] == pytest.approx(0.003)
    assert position["simulated_fees_usd"] == pytest.approx(0.6)
    assert position["dip_levels_hit"] == []
    assert "fee_accounting_complete" not in position
    assert trader._risk_reservations == {}


def test_hardcore_buy_the_dip_reserves_the_actual_simulated_fee(monkeypatch):
    class Store:
        def __init__(self, position):
            self.state = {"open_positions": [position]}
            self.save_calls = 0

        def save(self):
            self.save_calls += 1

    async def pair_data(_mint):
        return {"marketCap": 700}

    async def sol_rate():
        return 200.0

    monkeypatch.setattr(paper_trader, "_get_pair_data", pair_data)
    monkeypatch.setattr(paper_trader, "get_sol_usd_rate", sol_rate)
    monkeypatch.setattr(
        paper_trader, "_simulated_paper_fee_sol",
        lambda _buy_mode: paper_trader.HARDCORE_MAX_FEE_SOL,
    )
    monkeypatch.setattr(paper_trader.config, "MAX_OPEN_EXPOSURE_USD", 44.0)
    position = {
        "token_mint": "mint", "settings": {"buy_mode": "hardcore", "buy_the_dip_levels": [
            {"drop_pct": 20, "amount_sol": 0.1},
        ]},
        "buy_mode": "hardcore", "ath_market_cap": 1000,
        "dip_levels_hit": [], "units": 10.0, "cost_basis_usd": 20.0,
        "total_sol_invested": 0.103, "simulated_fees_sol": 0.003,
        "simulated_fees_usd": 0.6,
    }
    store = Store(position)
    trader = paper_trader.PaperTrader(store)

    asyncio.run(trader._check_buy_the_dip(position, 2.0))

    # The old generic fee cushion would have reserved about $21.10 and allowed
    # this add-on; the actual 0.027 SOL PAPER fee raises it to $26.40.
    assert position["units"] == pytest.approx(10.0)
    assert position["cost_basis_usd"] == pytest.approx(20.0)
    assert position["dip_levels_hit"] == []
    assert store.save_calls == 0
    assert trader._risk_reservations == {}


def test_concurrent_buy_the_dip_checks_apply_one_level_once(monkeypatch):
    class Store:
        def __init__(self, position):
            self.state = {"open_positions": [position]}
            self.save_calls = 0

        def save(self):
            self.save_calls += 1

    async def pair_data(_mint):
        return {"marketCap": 700}

    position = {
        "token_mint": "mint", "settings": {"buy_mode": "simple", "buy_the_dip_levels": [
            {"drop_pct": 20, "amount_sol": 0.1},
        ]},
        "buy_mode": "simple", "ath_market_cap": 1000,
        "dip_levels_hit": [], "units": 10.0, "cost_basis_usd": 20.0,
        "total_sol_invested": 0.103, "simulated_fees_sol": 0.003,
        "simulated_fees_usd": 0.6,
    }
    store = Store(position)
    trader = paper_trader.PaperTrader(store)

    async def run_concurrent_checks():
        first_rate_started = asyncio.Event()
        release_first_rate = asyncio.Event()
        rate_calls = 0

        async def sol_rate():
            nonlocal rate_calls
            rate_calls += 1
            if rate_calls == 1:
                first_rate_started.set()
                await release_first_rate.wait()
                return 300.0
            return 200.0

        monkeypatch.setattr(paper_trader, "_get_pair_data", pair_data)
        monkeypatch.setattr(paper_trader, "get_sol_usd_rate", sol_rate)
        first = asyncio.create_task(trader._check_buy_the_dip(position, 2.0))
        await first_rate_started.wait()
        second = asyncio.create_task(trader._check_buy_the_dip(position, 2.0))
        await second
        release_first_rate.set()
        await first
        return rate_calls

    assert asyncio.run(run_concurrent_checks()) == 2
    assert position["units"] == pytest.approx(20.0)
    assert position["cost_basis_usd"] == pytest.approx(40.6)
    assert position["total_sol_invested"] == pytest.approx(0.206)
    assert position["dip_levels_hit"] == [0]
    assert store.save_calls == 1
    assert trader._risk_reservations == {}


def test_entry_risk_refuses_duplicate_active_reservation_id(monkeypatch):
    class Store:
        state = {"open_positions": []}

    async def sol_rate():
        return 200.0

    monkeypatch.setattr(paper_trader, "get_sol_usd_rate", sol_rate)
    trader = paper_trader.PaperTrader(Store())
    trader._risk_reservations["dip:mint:position:0"] = 10.0

    result = asyncio.run(trader._reserve_entry_risk(
        "dip:mint:position:0",
        {"buy_amount_sol": 0.1},
        simulated_fee_sol=0.003,
    ))

    assert result == (False, "entry_already_reserved")
    assert trader._risk_reservations == {"dip:mint:position:0": 10.0}


def test_entry_risk_fails_closed_without_independently_verified_sol_usd(monkeypatch):
    class Store:
        state = {"open_positions": []}

    async def unavailable_rate():
        raise paper_trader.SolUsdPriceUnavailable("independent_provenance_unavailable")

    monkeypatch.setattr(paper_trader, "get_sol_usd_rate", unavailable_rate)
    trader = paper_trader.PaperTrader(Store())

    result = asyncio.run(
        trader._reserve_entry_risk("signal-1", {"buy_amount_sol": 0.1})
    )

    assert result == (False, "sol_usd_provenance_unverified")
    assert trader._risk_reservations == {}
    assert trader._risk_reservation_rates == {}


def test_sell_initials_books_fee_in_realized_pnl_and_net_sol_recovery(monkeypatch):
    class Store:
        def __init__(self):
            self.state = {"trades": [], "total_pnl_usd": 0.0, "session_losses_usd": 0.0}

        def save(self):
            pass

    rates = iter([200.0, 100.0])
    rate_calls = []

    async def sol_rate():
        rate_calls.append(True)
        return next(rates)

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
    assert rate_calls == [True]
    assert position["fee_accounting_complete"] is False
    assert store.state["trades"][0]["fee_accounting_complete"] is False
    assert store.state["trades"][0]["fee_accounting_note"] == "legacy_fee_history_unavailable"


def test_new_position_creation_marks_fee_accounting_version(monkeypatch):
    class Store:
        def __init__(self):
            self.state = {
                "emergency_stop": False,
                "session_losses_usd": 0.0,
                "session_losses_reset_at": time.time(),
                "open_positions": [],
                "closed_positions": [],
                "monitored_dev_wallets": {},
                "total_pnl_usd": 0.0,
            }

        def get_wallet_settings(self, _wallet):
            return {
                "buy_only_once": False,
                "buy_mode": "simple",
                "buy_amount_sol": 0.1,
                "snipe_delay_s": 0,
                "security_check_enabled": False,
            }

        def is_auto_buy_active(self, _wallet):
            return True

        def save(self):
            pass

        def record_trade_result(self, *_args, **_kwargs):
            return False

    async def pair_data(_mint):
        return {"priceUsd": "2", "marketCap": "5000"}

    async def sol_rate():
        return 200.0

    async def buy(_mint, entry_price, _settings):
        return {
            "units": 10.0, "cost_basis_usd": 20.0, "actual_price": entry_price,
            "signature": None, "sol_usd_rate": 200.0,
        }

    store = Store()
    trader = paper_trader.PaperTrader(store)
    async def allow_risk(_reservation, _settings, **_kwargs):
        return True, None

    monkeypatch.setattr(paper_trader.config, "TRADING_MODE", "PAPER")
    monkeypatch.setattr(paper_trader, "get_bonding_curve_price", lambda _mint: _no_onchain_price())
    monkeypatch.setattr(paper_trader, "_get_pair_data", pair_data)
    monkeypatch.setattr(paper_trader, "get_sol_usd_rate", sol_rate)
    monkeypatch.setattr(trader, "_execute_buy", buy)
    monkeypatch.setattr(trader, "_reserve_entry_risk", allow_risk)
    monkeypatch.setattr(paper_trader.asyncio, "create_task", lambda coroutine: coroutine.close())

    position = asyncio.run(trader.open_position("token-mint", "source-wallet", "test"))

    assert position["fee_accounting_version"] == paper_trader.FEE_ACCOUNTING_VERSION
    assert position["fee_accounting_complete"] is True
    assert "fee_accounting_note" not in position

    position["settings"]["buy_the_dip_levels"] = [{"drop_pct": 20, "amount_sol": 0.1}]

    async def dip_pair_data(_mint):
        return {"marketCap": 3500}

    monkeypatch.setattr(paper_trader, "_get_pair_data", dip_pair_data)
    asyncio.run(trader._check_buy_the_dip(position, 2.0))
    assert position["fee_accounting_complete"] is True

    asyncio.run(trader._close_remaining(position, 2.0, 0.0, "TEST"))

    closed = store.state["closed_positions"][0]
    assert closed["fee_accounting_version"] == paper_trader.FEE_ACCOUNTING_VERSION
    assert closed["fee_accounting_complete"] is True
    assert "fee_accounting_note" not in closed


async def _no_onchain_price():
    return None


def test_legacy_position_stays_marked_incomplete_when_closed(monkeypatch):
    class Store:
        def __init__(self, position):
            self.state = {
                "open_positions": [position], "closed_positions": [],
                "total_pnl_usd": 0.0, "session_losses_usd": 0.0,
            }

        def save(self):
            pass

        def record_trade_result(self, *_args, **_kwargs):
            return False

    position = {
        "token_mint": "mint", "source_wallet": "source", "settings": {},
        "units": 10.0, "cost_basis_usd": 11.0, "entry_time": 1.0,
        "simulated_fees_sol": 0.003,
    }
    store = Store(position)
    trader = paper_trader.PaperTrader(store)

    async def execute_sell(*_args):
        return {
            "proceeds_usd": 12.0, "sol_cashflow": 0.05, "signature": None,
            "simulated_fee_sol": 0.003, "simulated_fee_usd": 0.6,
        }

    monkeypatch.setattr(paper_trader.config, "TRADING_MODE", "PAPER")
    monkeypatch.setattr(trader, "_execute_sell", execute_sell)

    asyncio.run(trader._close_remaining(position, 1.2, 20.0, "TP"))

    assert store.state["closed_positions"][0]["fee_accounting_complete"] is False
    assert store.state["closed_positions"][0]["fee_accounting_note"] == "legacy_fee_history_unavailable"
    assert "fee_accounting_version" not in store.state["closed_positions"][0]
    assert store.state["pnl_fee_history_incomplete"] is True
    assert store.state["pnl_fee_history_warning_version"] == 1
    assert store.state["total_pnl_usd"] == pytest.approx(0.4)
