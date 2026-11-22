import asyncio

import telegram_bot
from telegram_bot import SniperTelegramBot


class _Message:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


class _Update:
    def __init__(self):
        self.effective_message = _Message()
        self.message = self.effective_message
        self.effective_user = None


def test_performance_command_displays_sanitized_metrics(monkeypatch):
    monkeypatch.setattr(telegram_bot, "format_metrics_report", lambda: "rpc.request — n=4")
    bot = SniperTelegramBot(object(), object(), object())
    update = _Update()
    asyncio.run(bot.cmd_performance(update, object()))
    text, kwargs = update.effective_message.replies[0]
    assert "rpc.request" in text
    assert "parse_mode" not in kwargs


def test_performance_command_keeps_stage_underscores_literal(monkeypatch):
    monkeypatch.setattr(telegram_bot, "format_metrics_report", lambda: "strategy.open_position — p95=2 ms")
    bot = SniperTelegramBot(object(), object(), object())
    update = _Update()
    asyncio.run(bot.cmd_performance(update, object()))
    text, kwargs = update.effective_message.replies[0]
    assert "strategy.open_position" in text
    assert "parse_mode" not in kwargs


def test_reconcile_command_is_read_only_and_reports_results(monkeypatch):
    class Store:
        state = {"pending_transactions": [{"signature": "public-signature"}], "emergency_stop": False}

        def activate_emergency_stop(self, _reason):
            self.state["emergency_stop"] = True

    async def reconcile(store):
        assert store is bot.data_store
        return {
            "checked": 1,
            "resolved_from_positions": 0,
            "finalized_failures_resolved": 0,
            "confirmed_unaccounted": 1,
            "pending_or_unknown": 0,
            "invalid": 0,
            "deferred": 0,
            "remaining": 1,
        }

    monkeypatch.setattr(telegram_bot, "reconcile_pending_transactions_readonly", reconcile)
    store = Store()
    bot = SniperTelegramBot(store, object(), object())
    update = _Update()
    asyncio.run(bot.cmd_reconcile(update, object()))
    text, _kwargs = update.effective_message.replies[0]
    assert "Succès finalisés sans position durable : 1" in text
    assert "Aucun swap n'a été envoyé" in text
    assert store.state["pending_transactions"]
    assert store.state["emergency_stop"] is True


def test_reconcile_command_does_not_call_rpc_when_no_signatures(monkeypatch):
    class Store:
        state = {"pending_transactions": []}

    async def unexpected_reconcile(*_args, **_kwargs):
        raise AssertionError("RPC reconciliation must not run without pending signatures")

    monkeypatch.setattr(telegram_bot, "reconcile_pending_transactions_readonly", unexpected_reconcile)
    bot = SniperTelegramBot(Store(), object(), object())
    update = _Update()
    asyncio.run(bot.cmd_reconcile(update, object()))
    assert "Aucune transaction" in update.effective_message.replies[0][0]


def test_reconcile_malformed_state_activates_stop_without_rpc():
    class Store:
        state = {"pending_transactions": None}

        def activate_emergency_stop(self, _reason):
            self.state["emergency_stop"] = True

    bot = SniperTelegramBot(Store(), object(), object())
    update = _Update()
    asyncio.run(bot.cmd_reconcile(update, object()))

    assert bot.data_store.state["emergency_stop"] is True
    assert "aucune requête RPC" in update.effective_message.replies[0][0]


def test_halt_reports_failed_durable_write_without_showing_exception_text():
    class Store:
        def activate_emergency_stop(self, _reason):
            raise OSError("/private-path-placeholder")

    bot = SniperTelegramBot(Store(), object(), object())
    update = _Update()
    asyncio.run(bot.cmd_halt(update, object()))

    reply = update.effective_message.replies[0][0]
    assert "activé en mémoire" in reply
    assert "private-path-placeholder" not in reply
