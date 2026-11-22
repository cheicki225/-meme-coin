import asyncio
import logging

from notifier import Notifier


class Store:
    state = {"telegram_chat_id": 123}


class Bot:
    def __init__(self):
        self.calls = 0

    async def send_message(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("request failed at https://api.telegram.org/botTOKEN_MUST_STAY_PRIVATE/sendMessage")


def test_telegram_exception_does_not_leak_bot_token(caplog):
    bot = Bot()
    caplog.set_level(logging.WARNING, logger="notifier")
    delivered = asyncio.run(Notifier(Store(), bot).notify("risk_alert", "safe_text"))
    assert bot.calls == 2
    assert delivered is True
    assert "TOKEN_MUST_STAY_PRIVATE" not in caplog.text


def test_disconnected_notifier_does_not_log_message_body(caplog):
    caplog.set_level(logging.INFO, logger="notifier")
    delivered = asyncio.run(Notifier(Store()).notify("risk_alert", "private_wallet_detail"))

    assert delivered is False
    assert "private_wallet_detail" not in caplog.text
    assert "Telegram non connecté" in caplog.text


def test_telegram_delivery_failure_returns_false_without_exception_text(caplog):
    class FailingBot:
        async def send_message(self, **kwargs):
            raise RuntimeError("bot token should not appear in logs")

    caplog.set_level(logging.ERROR, logger="notifier")
    delivered = asyncio.run(Notifier(Store(), FailingBot()).notify("risk_alert", "safe"))

    assert delivered is False
    assert "bot token should not appear" not in caplog.text
