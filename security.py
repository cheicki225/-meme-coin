"""
════════════════════════════════════════════════════════════════
SÉCURITÉ TOKEN — Vérification via GoPlus Security API
════════════════════════════════════════════════════════════════
Reprend la logique de l'ancien bot.py (check_security_full) adaptée
à ce projet. Vérifie honeypot, taxes d'achat/vente, verrouillage de
LP avant d'valider un achat.

Note : sur Pump.fun, le risque de honeypot classique est déjà limité
par la bonding curve (pas de LP externe à manipuler tant que le token
n'a pas "gradué" vers Raydium) — voir les vidéos Faomo à ce sujet.
Cette vérification devient surtout utile si tu élargis au-delà de
Pump.fun, ou pour les tokens ayant déjà gradué.

Désactivé par défaut par wallet (security_check_enabled=False dans
config.DEFAULT_WALLET_SETTINGS) — à activer explicitement si voulu.
"""

import logging
import aiohttp

import config
import rpc_client

log = logging.getLogger("security")


async def check_token_security(token_mint: str) -> dict:
    """
    Retourne : {
        "is_honeypot": bool,
        "buy_tax": float, "sell_tax": float,
        "lp_locked_pct": float,
        "owner_renounced": bool,
        "security_score": int (0-100),
        "error": str | None,
    }
    """
    if not config.GOPLUS_API_KEY and not config.GOPLUS_API_SECRET:
        # Pas de clé configurée : on ne bloque jamais silencieusement un achat,
        # on retourne un score neutre avec une erreur explicite dans les logs.
        log.debug("Aucune clé GoPlus configurée — vérification sécurité ignorée.")
        return {"error": "no_api_key", "security_score": 50, "is_honeypot": False}

    url = f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={token_mint}"
    headers = {"Accept": "application/json"}
    if config.GOPLUS_API_KEY:
        headers["x-api-key"] = config.GOPLUS_API_KEY
    if config.GOPLUS_API_SECRET:
        headers["Authorization"] = f"Bearer {config.GOPLUS_API_SECRET}"

    try:
        async with aiohttp.ClientSession(connector=rpc_client.get_http_connector(), connector_owner=False) as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    log.debug(f"GoPlus statut {resp.status} pour {token_mint[:8]}...")
                    return {"error": f"http_{resp.status}", "security_score": 50, "is_honeypot": False}
                data = await resp.json()
    except Exception as e:
        log.debug(f"Erreur GoPlus pour {token_mint[:8]}...: {e}")
        return {"error": str(e), "security_score": 50, "is_honeypot": False}

    result = data.get("result", {})
    token_data = result.get(token_mint.lower(), result.get(token_mint, {}))

    if not token_data:
        # Très courant pour un token Pump.fun tout juste créé — pas encore indexé
        return {"error": "not_indexed", "security_score": 50, "is_honeypot": False}

    buy_tax = _safe_float(token_data.get("buy_tax", 0))
    sell_tax = _safe_float(token_data.get("sell_tax", 0))
    lp_locked = _safe_float(token_data.get("lp_locked_percent", token_data.get("lp_locked", 0)))
    honeypot = str(token_data.get("is_honeypot", "0")) == "1"
    renounced = str(token_data.get("is_open_source", "0")) == "1"
    mintable = str(token_data.get("is_mintable", "0")) == "1"

    score = 100
    if honeypot:
        score = 0
    else:
        if sell_tax > 20:
            score -= 50
        elif sell_tax > 10:
            score -= 25
        elif sell_tax > 5:
            score -= 10
        if buy_tax > 10:
            score -= 10
        if lp_locked < 50:
            score -= 35
        elif lp_locked < 80:
            score -= 15
        if mintable:
            score -= 15
        if renounced:
            score += 10
    score = max(0, min(100, score))

    return {
        "is_honeypot": honeypot,
        "buy_tax": buy_tax,
        "sell_tax": sell_tax,
        "lp_locked_pct": lp_locked,
        "owner_renounced": renounced,
        "is_mintable": mintable,
        "security_score": score,
        "error": None,
    }


def _safe_float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
