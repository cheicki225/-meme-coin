import asyncio
import math
import time

import pytest

import config
import paper_trader
from paper_trader import PaperTrader


class _Store:
    def __init__(self, position):
        self.state = {
            "open_positions": [position],
            "closed_positions": [],
            "total_pnl_usd": 7.5,
            "session_losses_usd": 0.0,
        }
        self.save_calls = 0
        self.fail_save = False

    def save(self):
        self.save_calls += 1
        if self.fail_save:
            raise OSError("simulated persistence failure")


def _position():
    return {
        "token_mint": "test-token-mint",
        "source_wallet": "test-source-wallet",
        "entry_price": 2.0,
        "entry_time": time.time() - 3600,
        "units": 10.0,
        "cost_basis_usd": 20.0,
        "settings": {},
    }


@pytest.mark.parametrize("price", [None, 0, -1, True, "2.0", math.nan, math.inf, 10**400])
def test_expiration_without_valid_exit_price_stays_open_and_marks_unknown(monkeypatch, price):
    position = _position()
    store = _Store(position)
    trader = PaperTrader(store)
    close_calls = []

    async def get_price(_token_mint):
        return {"price": price, "market_cap": 0}

    async def verified_sol_usd_rate():
        return 100.0

    async def close(*args):
        close_calls.append(args)

    monkeypatch.setattr(paper_trader, "get_live_price_and_market_cap", get_price)
    monkeypatch.setattr(paper_trader, "get_sol_usd_rate", verified_sol_usd_rate)
    monkeypatch.setattr(trader, "_close_remaining", close)

    asyncio.run(trader._monitor_position(position, max_duration_s=0))

    assert close_calls == []
    assert store.state["open_positions"] == [position]
    assert store.state["closed_positions"] == []
    assert position["units"] == 10.0
    assert position["cost_basis_usd"] == 20.0
    assert position["exit_status"] == "unknown"
    assert position["exit_pending_reason"] == "exit_price_unavailable"
    assert position["exit_request_reason"] == "EXPIRATION"
    assert store.state["total_pnl_usd"] == 7.5
    assert store.save_calls == 1


def test_expiration_with_valid_price_requests_close(monkeypatch):
    position = _position()
    store = _Store(position)
    trader = PaperTrader(store)
    close_calls = []

    async def get_price(_token_mint):
        return {"price": 3.0, "market_cap": 0}

    async def close(position_arg, price, change_pct, reason):
        close_calls.append((position_arg, price, change_pct, reason))

    async def verified_sol_usd_rate():
        return 100.0

    monkeypatch.setattr(paper_trader, "get_live_price_and_market_cap", get_price)
    monkeypatch.setattr(paper_trader, "get_sol_usd_rate", verified_sol_usd_rate)
    monkeypatch.setattr(trader, "_close_remaining", close)

    asyncio.run(trader._monitor_position(position, max_duration_s=0))

    assert close_calls == [(position, 3.0, 50.0, "EXPIRATION")]


def test_direct_close_with_invalid_price_does_not_realize_pnl():
    position = _position()
    store = _Store(position)
    trader = PaperTrader(store)

    asyncio.run(trader._close_remaining(position, None, 0.0, "MANUAL_SELL"))

    assert store.state["open_positions"] == [position]
    assert store.state["closed_positions"] == []
    assert position["units"] == 10.0
    assert position["cost_basis_usd"] == 20.0
    assert position["exit_status"] == "unknown"
    assert store.state["total_pnl_usd"] == 7.5


def test_unavailable_exit_price_stays_open_when_pending_state_cannot_be_saved():
    position = _position()
    store = _Store(position)
    store.fail_save = True
    trader = PaperTrader(store)

    asyncio.run(trader._close_remaining(position, None, 0.0, "EXPIRATION"))

    assert store.state["open_positions"] == [position]
    assert position["units"] == 10.0
    assert position["cost_basis_usd"] == 20.0
    assert position["exit_status"] == "unknown"
    assert store.state["total_pnl_usd"] == 7.5
    assert store.save_calls == 1


def test_paper_startup_resumes_persisted_position_monitors_once(monkeypatch):
    monkeypatch.setattr(config, "TRADING_MODE", "PAPER")
    position = _position()
    position["entry_time"] = time.time() - 3590
    trader = PaperTrader(_Store(position))
    started = []

    async def monitor(position_arg, *, max_duration_s=3600):
        started.append((position_arg, max_duration_s))

    monkeypatch.setattr(trader, "_monitor_position", monitor)

    async def scenario():
        assert trader.resume_open_positions() == 1
        assert trader.resume_open_positions() == 0
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert len(started) == 1
    assert started[0][0] is position
    assert 0 <= started[0][1] <= 10


@pytest.mark.parametrize(
    "entry_time",
    [None, 0, -1, math.nan, math.inf, 10**400, time.time() + 3600],
)
def test_startup_does_not_restart_unknown_or_invalid_position_age(entry_time, monkeypatch):
    monkeypatch.setattr(config, "TRADING_MODE", "PAPER")
    position = _position()
    position["entry_time"] = entry_time
    store = _Store(position)
    trader = PaperTrader(store)

    assert trader.resume_open_positions() == 0
    assert trader._position_monitor_tasks == {}
    assert position["exit_status"] == "unknown"
    assert position["exit_pending_reason"] == "entry_time_unavailable"
    assert store.state["open_positions"] == [position]
    assert store.save_calls == 1


def test_non_paper_mode_does_not_resume_persisted_monitors(monkeypatch):
    monkeypatch.setattr(config, "TRADING_MODE", "UNKNOWN")
    position = _position()
    trader = PaperTrader(_Store(position))

    assert trader.resume_open_positions() == 0
    assert trader._position_monitor_tasks == {}
