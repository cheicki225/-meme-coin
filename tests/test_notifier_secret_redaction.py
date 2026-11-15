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
    asyncio.run(Notifier(Store(), bot).notify("risk_alert", "safe text"))
    assert bot.calls == 2
    assert "TOKEN_MUST_STAY_PRIVATE" not in caplog.text
