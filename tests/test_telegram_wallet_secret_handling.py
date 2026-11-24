"""Private-key handling regressions for Telegram wallet import."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import telegram_bot
from telegram_bot import SniperTelegramBot


class _Message:
    def __init__(self, text: str, *, delete_error: Exception | None = None):
        self.text = text
        self.delete_error = delete_error
        self.deleted = False

    async def delete(self) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted = True


class _Bot:
    def __init__(self):
        self.sent_messages: list[dict[str, object]] = []

    async def send_message(self, **kwargs) -> None:
        self.sent_messages.append(kwargs)


class _WalletManager:
    def __init__(self, data_store):
        self.data_store = data_store
        self.imports: list[tuple[str, str]] = []

    def import_wallet(self, label: str, private_key: str) -> dict[str, str]:
        self.imports.append((label, private_key))
        return {"label": label, "pubkey": "public-address"}


def _setup(monkeypatch, message: _Message):
    chat_id = 900001
    user_states = telegram_bot.user_states
    monkeypatch.setitem(
        user_states,
        chat_id,
        {"awaiting": "import_wallet_privatekey", "wallet_label": "test-wallet"},
    )
    wallet_managers: list[_WalletManager] = []

    class _WalletManagerFactory(_WalletManager):
        def __init__(self, data_store):
            super().__init__(data_store)
            wallet_managers.append(self)

    monkeypatch.setitem(
        sys.modules,
        "wallet_manager",
        SimpleNamespace(WalletManager=_WalletManagerFactory),
    )
    bot_client = _Bot()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        message=message,
    )
    context = SimpleNamespace(bot=bot_client)
    bot = SniperTelegramBot(object(), object(), object())
    return bot, update, context, bot_client, wallet_managers, user_states, chat_id


def test_failed_private_key_message_deletion_does_not_import_or_log_key(monkeypatch, caplog):
    private_key = "test-private-key-not-real"
    message = _Message(
        private_key,
        delete_error=RuntimeError(f"telegram rejection containing {private_key}"),
    )
    bot, update, context, bot_client, managers, states, chat_id = _setup(
        monkeypatch, message
    )

    asyncio.run(bot.on_text(update, context))

    assert not message.deleted
    assert managers[0].imports == []
    assert chat_id not in states
    sent_text = " ".join(str(item["text"]) for item in bot_client.sent_messages)
    assert "clé n'a pas été importée" in sent_text
    assert private_key not in sent_text
    assert private_key not in caplog.text


def test_private_key_is_imported_only_after_message_deletion_succeeds(monkeypatch):
    private_key = "test-private-key-not-real"
    message = _Message(private_key)
    bot, update, context, bot_client, managers, states, chat_id = _setup(
        monkeypatch, message
    )

    asyncio.run(bot.on_text(update, context))

    assert message.deleted
    assert managers[0].imports == [("test-wallet", private_key)]
    assert chat_id not in states
    assert bot_client.sent_messages[0]["text"] == (
        "✅ Wallet *'test-wallet'* importé : `public-address`"
    )
    assert private_key not in bot_client.sent_messages[0]["text"]
