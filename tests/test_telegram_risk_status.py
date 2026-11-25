import asyncio

import backtest
import telegram_bot


class _Store:
    def __init__(self, state):
        self.state = state
        self.save_count = 0

    def save(self):
        self.save_count += 1

    def count_wallets_by_mode(self):
        return 2, 1

    def count_wallets(self):
        return 3


class _Message:
    def __init__(self):
        self.text = None
        self.kwargs = None

    async def reply_text(self, text, **kwargs):
        self.text = text
        self.kwargs = kwargs


def _bot_with_state(state):
    bot = object.__new__(telegram_bot.SniperTelegramBot)
    bot.data_store = _Store(state)
    return bot


def test_stats_distinguishes_manual_emergency_stop_from_loss_breaker_and_pending_tx(monkeypatch):
    monkeypatch.setattr(telegram_bot.config, "CIRCUIT_BREAKER_USD", 100)
    state = {
        "total_pnl_usd": 0,
        "session_losses_usd": 12,
        "open_positions": [],
        "closed_positions": [],
        "emergency_stop": True,
        "emergency_stop_reason": "manual Telegram halt by user 42",
        "pending_transactions": [{"signature": "do-not-display-this-signature"}],
    }
    message = _Message()
    update = type("Update", (), {"message": message})()

    asyncio.run(_bot_with_state(state).cmd_stats(update, None))

    assert "Circuit breaker pertes* : ⚪ état inconnu" in message.text
    assert "Arrêt d'urgence : 🛑 ACTIF — commande Telegram /halt" in message.text
    assert "Transactions en attente : ⚠️ 1 à réconcilier" in message.text
    assert "do-not-display-this-signature" not in message.text
    assert message.kwargs["parse_mode"] == "Markdown"


def test_stats_does_not_claim_manual_stop_when_only_loss_limit_is_reached(monkeypatch):
    monkeypatch.setattr(telegram_bot.config, "CIRCUIT_BREAKER_USD", 100)

    status = telegram_bot._format_risk_status(
        {
            "session_losses_usd": 100,
            "emergency_stop": False,
            "pending_transactions": [],
        },
        100,
    )

    assert "Circuit breaker pertes* : ⚪ état inconnu" in status
    assert "Arrêt d'urgence : 🟢 inactif" in status
    assert "Transactions en attente : aucune" in status


def test_stats_marks_historical_aggregate_pnl_as_potentially_incomplete(monkeypatch):
    monkeypatch.setattr(telegram_bot.config, "CIRCUIT_BREAKER_USD", 100)
    state = {
        "total_pnl_usd": 42.5,
        "session_losses_usd": 0,
        "open_positions": [],
        "closed_positions": [],
    }
    bot = _bot_with_state(state)
    message = _Message()
    update = type("Update", (), {"message": message})()

    asyncio.run(bot.cmd_stats(update, None))

    assert "P&L total: ⚪ inconnu (provenance SOL/USD non vérifiée)" in message.text
    assert "PnL historique potentiellement incomplet" in message.text
    assert state["pnl_fee_history_incomplete"] is True
    assert state["pnl_fee_history_warning_version"] == 1
    assert state["total_pnl_usd"] == 42.5
    assert bot.data_store.save_count == 1


def test_new_empty_state_has_no_legacy_pnl_warning(monkeypatch):
    monkeypatch.setattr(telegram_bot.config, "CIRCUIT_BREAKER_USD", 100)
    state = {
        "total_pnl_usd": 0.0,
        "session_losses_usd": 0.0,
        "open_positions": [],
        "closed_positions": [],
    }
    bot = _bot_with_state(state)
    message = _Message()
    update = type("Update", (), {"message": message})()

    asyncio.run(bot.cmd_stats(update, None))

    assert "frais anciens" not in message.text
    assert "pnl_fee_history_incomplete" not in state
    assert bot.data_store.save_count == 0


def test_session_stats_menu_displays_historical_pnl_warning(monkeypatch):
    monkeypatch.setattr(telegram_bot.config, "CIRCUIT_BREAKER_USD", 100)
    state = {
        "language": "en",
        "total_pnl_usd": 9.0,
        "session_losses_usd": 0.0,
        "open_positions": [],
        "closed_positions": [],
    }
    bot = _bot_with_state(state)
    captured = {}

    async def capture(_query, text, _keyboard, edit=True):
        captured["text"] = text

    bot._send_or_edit = capture
    asyncio.run(bot.show_session_stats(object()))

    assert "Historical PnL may be incomplete" in captured["text"]
    assert "unknown (SOL/USD provenance unverified)" in captured["text"]
    assert "Win rate: `⚪ unknown`" in captured["text"]
    assert state["pnl_fee_history_incomplete"] is True
    assert bot.data_store.save_count == 1


def test_refresh_pnl_reports_unknown_in_english_without_fetching_price():
    bot = _bot_with_state({"language": "en"})
    captured = {}

    class Query:
        async def answer(self, text, **kwargs):
            captured["text"] = text
            captured["kwargs"] = kwargs

    asyncio.run(bot._refresh_pnl(Query(), "fixture-mint"))

    assert captured["text"] == (
        "P&L unknown: SOL/USD provenance is not independently verified."
    )
    assert captured["kwargs"]["show_alert"] is True


def test_manual_exit_quote_failure_persists_unknown_provenance(monkeypatch):
    captured = {}
    bot = object.__new__(telegram_bot.SniperTelegramBot)

    class Trader:
        async def _record_exit_price_unknown(self, position, reason, *, pending_reason):
            captured["record"] = (position, reason, pending_reason)

    bot.trader = Trader()

    async def unavailable(_mint):
        raise backtest.SolUsdPriceUnavailable("independent_provenance_unavailable")

    monkeypatch.setattr(backtest, "get_live_price_and_market_cap", unavailable)
    position = {"token_mint": "fixture-mint"}

    result = asyncio.run(bot._load_exit_quote(position, "MANUAL_SELL"))

    assert result is None
    assert captured["record"] == (
        position,
        "MANUAL_SELL",
        "sol_usd_provenance_unverified",
    )


def test_stats_shows_unknown_state_instead_of_false_inactive_status():
    status = telegram_bot._format_risk_status(
        {
            "session_losses_usd": float("nan"),
            "emergency_stop": None,
            "pending_transactions": None,
        },
        100,
    )

    assert status.count("état inconnu") == 3
    assert "inactif" not in status
