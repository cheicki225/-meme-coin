import asyncio

import security
from telegram_bot import SniperTelegramBot


def test_telegram_security_command_displays_unknown_without_green_status(monkeypatch):
    edited = []

    class _Message:
        async def edit_text(self, text, **kwargs):
            edited.append((text, kwargs))

    class _UpdateMessage:
        async def reply_text(self, *_args, **_kwargs):
            return _Message()

    async def _check(_mint):
        return {"error": "not_indexed", "security_score": 0}

    monkeypatch.setattr(security, "check_token_security", _check)
    update = type("Update", (), {"message": _UpdateMessage()})()

    asyncio.run(SniperTelegramBot.check_security_inline(None, update, "TokenMint"))

    assert len(edited) == 1
    assert "inconnu / indisponible" in edited[0][0]
    assert "🟢" not in edited[0][0]
    assert "Faible" not in edited[0][0]
    assert "parse_mode" not in edited[0][1]


def test_telegram_security_command_displays_actual_solana_risks(monkeypatch):
    edited = []

    class _Message:
        async def edit_text(self, text, **kwargs):
            edited.append((text, kwargs))

    class _UpdateMessage:
        async def reply_text(self, *_args, **_kwargs):
            return _Message()

    result = {
        "error": None,
        "risk_model": "goplus_solana_authorities_v1",
        "security_score": 50,
        "is_mintable": True,
        "is_freezable": False,
        "is_closable": False,
        "metadata_mutable": False,
        "default_account_state": 1,
        "non_transferable": False,
        "transfer_fee_bps": 0,
        "transfer_hook_present": False,
        "max_dex_tvl_usd": 1_000.0,
        "lp_locked_pct": 100.0,
        "lp_lock_data_available": True,
        "is_honeypot": None,
        "honeypot_detection_available": False,
        "buy_tax": None,
        "sell_tax": None,
    }

    async def _check(_mint):
        return result

    monkeypatch.setattr(security, "check_token_security", _check)
    update = type("Update", (), {"message": _UpdateMessage()})()

    asyncio.run(SniperTelegramBot.check_security_inline(None, update, "TokenMint"))

    text = edited[0][0]
    assert "Autorité de mint active" in text
    assert "TVL DEX maximale rapportée: 1,000.00 USD" in text
    assert "Taxe buy/sell et détection honeypot EVM: non fournies par ce schéma Solana." in text
    assert "🟢" not in text
