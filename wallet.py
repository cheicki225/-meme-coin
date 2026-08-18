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
from backtest import get_sol_usd_rate

log = logging.getLogger("wallet")

_keypair_cache = None

# AJOUTÉ suite à une demande explicite : adresse officielle du mint USDC
# sur Solana — nécessaire pour détecter des transferts USDC (un token SPL,
# pas du SOL natif) et pour calculer la valeur d'un wallet en USDC.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


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
    """Retourne le solde SOL actuel du wallet d'exécution (celui du bot lui-même)."""
    pubkey = get_public_key()
    return await get_sol_balance_of(pubkey)


async def get_sol_balance_of(address: str) -> float:
    """
    AJOUTÉ pour la détection de transfert SOL important d'un dev surveillé
    (copytrade_listener.py) — version générique de get_sol_balance(), qui
    elle est câblée en dur sur le wallet du bot lui-même (SOLANA_PRIVATE_KEY).
    """
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getBalance",
        "params": [address],
    }
    result = await rpc_client.rpc_post(payload, timeout=10)
    lamports = result.get("value", 0) if isinstance(result, dict) else 0
    return lamports / 1_000_000_000


async def get_usdc_balance_of(address: str) -> float:
    """
    AJOUTÉ suite à une demande explicite : solde USDC d'une adresse
    (nécessite getTokenAccountsByOwner filtré par mint, contrairement au
    SOL natif qui utilise getBalance). Additionne tous les comptes de
    token USDC trouvés (normalement un seul, mais pas garanti).
    """
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [address, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}],
    }
    result = await rpc_client.rpc_post(payload, timeout=10)
    accounts = result.get("value", []) if isinstance(result, dict) else []

    total = 0.0
    for acc in accounts:
        try:
            ui_amount = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"]
            total += float(ui_amount or 0)
        except (KeyError, TypeError):
            continue
    return total


async def get_wallet_value_summary(address: str) -> dict:
    """
    AJOUTÉ suite à une demande explicite : valeur totale d'un wallet, en
    SOL + USDC UNIQUEMENT — pas tous les tokens détenus. Volontairement
    limité à ces 2 actifs : calculer la valeur de CHAQUE token détenu
    nécessiterait un appel de prix (DexScreener ou équivalent) par token,
    trop coûteux à faire en temps réel à chaque alerte. Cohérent avec la
    portée des alertes de retrait elles-mêmes, limitées à SOL + USDC.

    Retourne {"sol_balance": float, "sol_value_usd": float,
    "usdc_balance": float, "total_value_usd": float}.
    """
    sol_balance = await get_sol_balance_of(address)
    usdc_balance = await get_usdc_balance_of(address)
    sol_value_usd = sol_balance * await get_sol_usd_rate()
    total_value_usd = sol_value_usd + usdc_balance  # 1 USDC ≈ 1$

    return {
        "sol_balance": sol_balance,
        "sol_value_usd": sol_value_usd,
        "usdc_balance": usdc_balance,
        "total_value_usd": total_value_usd,
    }
