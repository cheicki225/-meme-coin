import asyncio
from types import SimpleNamespace

from telegram_bot import SniperTelegramBot


class _Message:
    def __init__(self):
        self.text = None

    async def reply_text(self, text):
        self.text = text


def test_resume_refusal_truncates_transaction_signatures():
    signature = "public-transaction-identifier-which-must-not-be-fully-displayed"
    bot = SniperTelegramBot.__new__(SniperTelegramBot)
    bot.data_store = SimpleNamespace(
        state={"pending_transactions": [{"signature": signature}]}
    )
    message = _Message()
    update = SimpleNamespace(effective_message=message)

    asyncio.run(bot.cmd_resume(update, None))

    assert signature not in message.text
    assert signature[:12] in message.text
