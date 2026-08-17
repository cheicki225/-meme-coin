"""
════════════════════════════════════════════════════════════════
WALLET — Chargement du keypair Solana pour l'exécution LIVE
════════════════════════════════════════════════════════════════
Règle absolue : la clé privée (config.SOLANA_PRIVATE_KEY) ne doit
JAMAIS apparaître dans un log, une exception non filtrée, ou un
message Telegram. Ce module ne l'expose que sous forme de Keypair
(objet solders), jamais en texte.
"""

import logging

from solders.keypair import Keypair
from solders.pubkey import Pubkey

import config
import rpc_client

log = logging.getLogger("wallet")

_keypair_cache = None


def load_keypair() -> Keypair:
    """Charge le keypair depuis config.SOLANA_PRIVATE_KEY (base58). Levée
    d'exception si la clé est absente ou invalide — ne jamais logguer la clé
    elle-même dans le message d'erreur."""
    global _keypair_cache
    if _keypair_cache is not None:
        return _keypair_cache

    if not config.SOLANA_PRIVATE_KEY:
        raise ValueError("SOLANA_PRIVATE_KEY n'est pas configurée — impossible de charger le wallet LIVE.")

    try:
        _keypair_cache = Keypair.from_base58_string(config.SOLANA_PRIVATE_KEY)
    except Exception:
        # Ne jamais inclure la clé elle-même dans le message d'erreur
        raise ValueError("SOLANA_PRIVATE_KEY invalide (format base58 attendu).")

    log.info(f"Wallet LIVE chargé — adresse publique : {_keypair_cache.pubkey()}")
    return _keypair_cache


def get_public_key() -> str:
    return str(load_keypair().pubkey())


async def get_sol_balance() -> float:
    """Retourne le solde SOL actuel du wallet d'exécution."""
    pubkey = get_public_key()
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getBalance",
        "params": [pubkey],
    }
    result = await rpc_client.rpc_post(payload, timeout=10)
    lamports = result.get("value", 0) if isinstance(result, dict) else 0
    return lamports / 1_000_000_000
