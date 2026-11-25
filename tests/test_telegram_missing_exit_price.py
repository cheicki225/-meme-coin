import asyncio
import time

import backtest
from paper_trader import PaperTrader
from telegram_bot import SniperTelegramBot


class _Store:
    def __init__(self, position):
        self.state = {
            "open_positions": [position],
            "closed_positions": [],
            "total_pnl_usd": 0.0,
            "session_losses_usd": 0.0,
        }
        self.save_calls = 0

    def save(self):
        self.save_calls += 1


class _Query:
    def __init__(self):
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


def _position():
    return {
        "token_mint": "test-token-mint",
        "source_wallet": "test-source-wallet",
        "entry_price": 2.0,
        "entry_time": time.time(),
        "units": 10.0,
        "cost_basis_usd": 20.0,
        "settings": {},
    }


def test_quick_sell_without_price_keeps_position_and_does_not_claim_execution(monkeypatch):
    position = _position()
    store = _Store(position)
    bot = SniperTelegramBot.__new__(SniperTelegramBot)
    bot.data_store = store
    bot.trader = PaperTrader(store)
    query = _Query()

    async def missing_price(_token_mint):
        return {"price": None, "market_cap": 0}

    monkeypatch.setattr(backtest, "get_live_price_and_market_cap", missing_price)

    asyncio.run(bot._quick_sell_by_mint(query, position["token_mint"], 50))

    assert store.state["open_positions"] == [position]
    assert store.state["closed_positions"] == []
    assert position["units"] == 10.0
    assert store.state["total_pnl_usd"] == 0.0
    assert position["exit_status"] == "unknown"
    assert query.answers
    assert "aucun PnL réalisé" in query.answers[0][0]


def test_refresh_pnl_reports_unknown_when_price_is_missing(monkeypatch):
    position = _position()
    store = _Store(position)
    bot = SniperTelegramBot.__new__(SniperTelegramBot)
    bot.data_store = store
    bot.trader = PaperTrader(store)
    query = _Query()

    async def missing_price(_token_mint):
        return {"price": 0, "market_cap": 0}

    monkeypatch.setattr(backtest, "get_live_price_and_market_cap", missing_price)

    asyncio.run(bot._refresh_pnl(query, position["token_mint"]))

    assert query.answers == [(
        "P&L inconnu : provenance SOL/USD non vérifiée indépendamment.",
        {"show_alert": True},
    )]
    assert store.state["total_pnl_usd"] == 0.0
