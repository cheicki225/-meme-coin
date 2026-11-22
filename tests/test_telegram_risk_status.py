import asyncio

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

    assert "Circuit breaker pertes* : 🟢 inactif" in message.text
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

    assert "Circuit breaker pertes* : 🛑 ACTIF" in status
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

    assert "42.50$" in message.text
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
    assert state["pnl_fee_history_incomplete"] is True
    assert bot.data_store.save_count == 1


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
