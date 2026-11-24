"""CallbackQuery authorization and malformed-input regressions."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from telegram_bot import SniperTelegramBot


class _CallbackQuery:
    def __init__(self, data):
        self.data = data
        self.answers = []

    async def answer(self, text=None, *, show_alert=False):
        self.answers.append((text, show_alert))


def _update(user_id=42, data="menu_main", *, chat_type="private"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id) if user_id is not None else None,
        effective_chat=SimpleNamespace(type=chat_type, id=1001),
        callback_query=_CallbackQuery(data) if data is not Ellipsis else None,
    )


def _bot():
    return SniperTelegramBot(object(), object(), object())


def test_authorized_private_callback_is_dispatched(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USER_IDS", frozenset({42}))
    bot = _bot()
    dispatched = []

    async def show_main_menu(query, *, edit):
        dispatched.append((query, edit))

    bot.show_main_menu = show_main_menu
    update = _update()

    asyncio.run(bot.on_button(update, object()))

    assert len(dispatched) == 1
    assert dispatched[0] == (update.callback_query, True)
    assert update.callback_query.answers == [(None, False)]


def test_unauthorized_callback_is_rejected_before_transfer_action(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USER_IDS", frozenset({42}))
    bot = _bot()
    executed = []

    async def execute_disperse(*args):
        executed.append(args)

    bot._execute_disperse = execute_disperse
    update = _update(user_id=99, data="confirmdisperse_wallet|destination|0.01")

    asyncio.run(bot.on_button(update, object()))

    assert executed == []
    assert update.callback_query.answers == [(
        "Accès refusé. Configurez TELEGRAM_ALLOWED_USER_IDS.", True
    )]


@pytest.mark.parametrize(
    "allowlist",
    [None, frozenset(), "42", frozenset({"42"})],
)
def test_absent_or_malformed_allowlist_fails_closed(monkeypatch, allowlist):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USER_IDS", allowlist)
    bot = _bot()
    update = _update()

    asyncio.run(bot.on_button(update, object()))

    assert update.callback_query.answers[0][1] is True
    assert not bot._is_authorized_update(update)


def test_group_callback_is_rejected_even_for_allowlisted_user(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USER_IDS", frozenset({42}))
    bot = _bot()
    update = _update(chat_type="group")

    asyncio.run(bot.on_button(update, object()))

    assert update.callback_query.answers[0][1] is True


@pytest.mark.parametrize(
    "data",
    [None, "", 12, "confirmdisperse_wallet", "confirmdisperse_wallet|destination|nan"],
)
def test_malformed_callback_is_answered_without_side_effects(monkeypatch, data):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USER_IDS", frozenset({42}))
    bot = _bot()
    executed = []

    async def execute_disperse(*args):
        executed.append(args)

    bot._execute_disperse = execute_disperse
    update = _update(data=data)

    asyncio.run(bot.on_button(update, object()))

    assert executed == []
    assert update.callback_query.answers == [("Action invalide.", True)]


def test_missing_callback_object_is_ignored(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USER_IDS", frozenset({42}))
    bot = _bot()
    update = _update(data=Ellipsis)

    asyncio.run(bot.on_button(update, object()))

    assert update.callback_query is None


def test_malformed_allowlist_environment_fails_configuration_load():
    child_env = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP")
        if key in os.environ
    }
    child_env.update({
        "PYTHON_DOTENV_DISABLED": "true",
        "TELEGRAM_ALLOWED_USER_IDS": "not-a-number",
    })

    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=Path(__file__).resolve().parents[1],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert "TELEGRAM_ALLOWED_USER_IDS" in result.stderr
