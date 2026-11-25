import asyncio
import time

import monitoring_list
import paper_trader
import pytest


def _valid_security_result(score=80):
    return {
        "error": None,
        "risk_model": "goplus_solana_authorities_v1",
        "security_score": score,
        "is_honeypot": None,
        "honeypot_detection_available": False,
        "buy_tax": None,
        "sell_tax": None,
        "is_mintable": False,
        "is_freezable": False,
        "is_closable": False,
        "metadata_mutable": False,
        "non_transferable": False,
        "default_account_state": 1,
        "transfer_fee_bps": 0,
        "transfer_hook_present": False,
        "max_dex_tvl_usd": 25_000.0,
        "lp_lock_data_available": True,
        "lp_locked_pct": 100.0,
    }


def test_wallet_settings_backfill_goplus_default_and_preserve_explicit_opt_out():
    store = object.__new__(monitoring_list.DataStore)
    store.state = {
        "monitored_dev_wallets": {
            "legacy-wallet": {"settings": {"buy_amount_sol": 0.1}},
            "opted-out-wallet": {"settings": {"security_check_enabled": False}},
        }
    }

    legacy = store.get_wallet_settings("legacy-wallet")
    opted_out = store.get_wallet_settings("opted-out-wallet")

    assert legacy["security_check_enabled"] is True
    assert opted_out["security_check_enabled"] is False


def test_monitor_persists_unknown_when_sol_usd_provenance_is_unavailable(monkeypatch):
    async def unavailable_rate():
        raise paper_trader.SolUsdPriceUnavailable("independent_provenance_unavailable")

    monkeypatch.setattr(paper_trader.config, "TRADING_MODE", "PAPER")
    monkeypatch.setattr(paper_trader, "get_sol_usd_rate", unavailable_rate)
    position = {
        "token_mint": "fixture-mint",
        "units": 1.0,
        "settings": {},
    }
    trader = paper_trader.PaperTrader(_Store({}))

    asyncio.run(trader._monitor_position(position, max_duration_s=1))

    assert position["exit_status"] == "unknown"
    assert position["exit_pending_reason"] == "sol_usd_provenance_unverified"


def test_paper_close_keeps_position_open_and_unknown_without_verified_rate(monkeypatch):
    async def unavailable_rate():
        raise paper_trader.SolUsdPriceUnavailable("independent_provenance_unavailable")

    monkeypatch.setattr(paper_trader.config, "TRADING_MODE", "PAPER")
    monkeypatch.setattr(paper_trader, "get_sol_usd_rate", unavailable_rate)
    position = {
        "token_mint": "fixture-mint",
        "units": 5.0,
        "cost_basis_usd": 10.0,
        "settings": {"buy_mode": "simple"},
    }
    trader = paper_trader.PaperTrader(_Store({}))

    asyncio.run(trader._close_remaining(position, 3.0, 25.0, "TAKE_PROFIT"))

    assert position["units"] == 5.0
    assert position["cost_basis_usd"] == 10.0
    assert position["exit_status"] == "unknown"
    assert position["exit_pending_reason"] == "sol_usd_provenance_unverified"


class _Store:
    def __init__(self, settings):
        self.state = {
            "emergency_stop": False,
            "session_losses_usd": 0.0,
            "session_losses_reset_at": time.time(),
        }
        self.settings = settings

    def get_wallet_settings(self, _wallet):
        return self.settings

    def is_auto_buy_active(self, _wallet):
        return True

    def has_already_bought(self, _wallet, _mint):
        return False

    def save(self):
        pass


@pytest.mark.parametrize(
    "security_result",
    [
        {"error": "no_api_key", "security_score": 0, "is_honeypot": False},
        {"error": "invalid_buy_tax", "security_score": 100, "is_honeypot": False},
        {"error": None, "security_score": 0, "is_honeypot": None},
        {"error": None, "security_score": 80},
    ],
)
def test_security_error_blocks_entry_even_when_min_score_is_zero(monkeypatch, security_result):
    settings = {
        "security_check_enabled": True,
        "min_security_score": 0,
        "buy_only_once": False,
        "buy_mode": "simple",
        "buy_amount_sol": 0.1,
    }
    trader = paper_trader.PaperTrader(_Store(settings))
    buy_calls = []

    async def pair_data(_mint):
        return {"priceUsd": "1", "marketCap": "10000"}

    async def security_check(_mint):
        return security_result

    async def execute_buy(*_args):
        buy_calls.append(True)
        raise AssertionError("security errors must block before simulated execution")

    monkeypatch.setattr(paper_trader.config, "TRADING_MODE", "PAPER")
    monkeypatch.setattr(paper_trader, "_get_pair_data", pair_data)
    monkeypatch.setattr(paper_trader.security_check, "check_token_security", security_check)
    trader._execute_buy = execute_buy

    result = asyncio.run(trader.open_position("token-mint", "source-wallet", "test"))

    assert result is None
    assert buy_calls == []


@pytest.mark.parametrize("minimum_score", [True, float("nan"), float("inf"), -1, 101, "0"])
def test_invalid_minimum_security_score_blocks_entry(monkeypatch, minimum_score):
    settings = {
        "security_check_enabled": True,
        "min_security_score": minimum_score,
        "buy_only_once": False,
        "buy_mode": "simple",
        "buy_amount_sol": 0.1,
    }
    trader = paper_trader.PaperTrader(_Store(settings))
    buy_calls = []

    async def pair_data(_mint):
        return {"priceUsd": "1", "marketCap": "10000"}

    async def security_check(_mint):
        return _valid_security_result()

    async def execute_buy(*_args):
        buy_calls.append(True)
        return {"units": 1.0, "cost_basis_usd": 1.0, "actual_price": 1.0}

    monkeypatch.setattr(paper_trader.config, "TRADING_MODE", "PAPER")
    monkeypatch.setattr(paper_trader, "_get_pair_data", pair_data)
    monkeypatch.setattr(paper_trader, "get_bonding_curve_price", lambda _mint: _no_price())
    monkeypatch.setattr(paper_trader.security_check, "check_token_security", security_check)
    trader._execute_buy = execute_buy

    result = asyncio.run(trader.open_position("token-mint", "source-wallet", "test"))

    assert result is None
    assert buy_calls == []


def test_malformed_goplus_setting_does_not_become_an_opt_out(monkeypatch):
    settings = {
        "security_check_enabled": None,
        "min_security_score": 0,
        "buy_only_once": False,
        "buy_mode": "simple",
        "buy_amount_sol": 0.1,
    }
    trader = paper_trader.PaperTrader(_Store(settings))
    buy_calls = []

    async def pair_data(_mint):
        return {"priceUsd": "1", "marketCap": "10000"}

    async def unavailable(_mint):
        return {"error": "provider_unavailable", "security_score": 0}

    async def execute_buy(*_args):
        buy_calls.append(True)
        raise AssertionError("malformed GoPlus setting must not bypass the security gate")

    monkeypatch.setattr(paper_trader.config, "TRADING_MODE", "PAPER")
    monkeypatch.setattr(paper_trader, "_get_pair_data", pair_data)
    monkeypatch.setattr(paper_trader.security_check, "check_token_security", unavailable)
    trader._execute_buy = execute_buy

    assert asyncio.run(trader.open_position("token-mint", "source-wallet", "test")) is None
    assert buy_calls == []


def test_low_goplus_dex_tvl_blocks_entry_before_execution(monkeypatch):
    settings = {
        "security_check_enabled": True,
        "min_security_score": 0,
        "buy_only_once": False,
        "buy_mode": "simple",
        "buy_amount_sol": 0.1,
    }
    trader = paper_trader.PaperTrader(_Store(settings))
    buy_calls = []

    async def pair_data(_mint):
        return {"priceUsd": "1", "marketCap": "10000"}

    async def security_check(_mint):
        return _valid_security_result()

    async def execute_buy(*_args):
        buy_calls.append(True)
        raise AssertionError("low reported DEX TVL must block entry")

    monkeypatch.setattr(paper_trader.config, "TRADING_MODE", "PAPER")
    monkeypatch.setattr(paper_trader.config, "GOPLUS_MIN_DEX_TVL_USD", 30_000.0)
    monkeypatch.setattr(paper_trader, "_get_pair_data", pair_data)
    monkeypatch.setattr(paper_trader, "get_bonding_curve_price", lambda _mint: _no_price())
    monkeypatch.setattr(paper_trader.security_check, "check_token_security", security_check)
    trader._execute_buy = execute_buy

    result = asyncio.run(trader.open_position("token-mint", "source-wallet", "test"))

    assert result is None
    assert buy_calls == []


async def _no_price():
    return None
