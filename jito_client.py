"""
════════════════════════════════════════════════════════════════
JITO CLIENT — Bundles avec tips pour accélérer l'inclusion
════════════════════════════════════════════════════════════════
Un "tip" Jito n'est PAS un priority fee classique (qui va au validateur
leader) — c'est un paiement séparé qui va à un compte Jito et sert de
signal principal dans l'enchère d'inclusion du bundle. Vérifié via
recherche web (docs Jito, mi-2026) : 95%+ du stake actif Solana tourne
le client Jito-Solana, donc c'est un vrai levier de vitesse, pas cosmétique.

Approche utilisée ici : bundle à 2 transactions (le swap + une transaction
de tip séparée), toutes deux signées par notre wallet et soumises ensemble
via sendBundle. C'est l'approche la plus simple à implémenter sans avoir à
recomposer l'instruction de swap déjà construite par Jupiter — la doc Jito
recommande d'intégrer le tip dans la transaction principale pour plus de
robustesse ("uncle bandit"), mais ça demanderait de décoder/recompiler le
message Jupiter, hors de portée raisonnable ici.

Les comptes de tip sont récupérés DYNAMIQUEMENT via getTipAccounts plutôt
que codés en dur — plus sûr, évite une adresse qui deviendrait invalide.
"""

import base64
import logging
import random
import time

import aiohttp
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import transfer, TransferParams
from solders.message import MessageV0
from solders.transaction import VersionedTransaction
from solders.hash import Hash

import config
import rpc_client

log = logging.getLogger("jito_client")

_tip_accounts_cache = {"accounts": [], "fetched_at": 0}
_TIP_ACCOUNTS_CACHE_TTL_S = 300  # les comptes changent rarement, cache 5 min pour éviter un aller-retour à chaque trade


class JitoError(Exception):
    pass


async def get_tip_accounts() -> list:
    """Récupère la liste actuelle des comptes de tip Jito (avec cache court)."""
    now = time.time()
    if _tip_accounts_cache["accounts"] and (now - _tip_accounts_cache["fetched_at"]) < _TIP_ACCOUNTS_CACHE_TTL_S:
        return _tip_accounts_cache["accounts"]

    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTipAccounts", "params": []}
    try:
        async with aiohttp.ClientSession(connector=rpc_client.get_http_connector(), connector_owner=False) as session:
            async with session.post(config.JITO_BLOCK_ENGINE_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    raise JitoError(f"getTipAccounts a échoué ({resp.status})")
                data = await resp.json()
                accounts = data.get("result", [])
                if not accounts:
                    raise JitoError(f"Réponse getTipAccounts vide/inattendue : {data}")
                _tip_accounts_cache["accounts"] = accounts
                _tip_accounts_cache["fetched_at"] = now
                return accounts
    except Exception as e:
        raise JitoError(f"Impossible de récupérer les comptes de tip Jito : {e}")


async def send_bundle_with_tip(swap_tx_bytes: bytes, keypair: Keypair, recent_blockhash: str,
                                tip_lamports: int = None) -> str:
    """
    Construit une transaction de tip séparée, bundle avec la transaction de
    swap déjà signée, et soumet le tout via sendBundle. Retourne l'ID du bundle
    (pas une signature de transaction classique — la confirmation se fait
    ensuite via la signature de la tx de swap elle-même, comme d'habitude).
    """
    tip_lamports = tip_lamports or config.JITO_TIP_LAMPORTS

    tip_accounts = await get_tip_accounts()
    tip_account = Pubkey.from_string(random.choice(tip_accounts))

    tip_ix = transfer(TransferParams(
        from_pubkey=keypair.pubkey(),
        to_pubkey=tip_account,
        lamports=tip_lamports,
    ))
    tip_msg = MessageV0.try_compile(
        payer=keypair.pubkey(),
        instructions=[tip_ix],
        address_lookup_table_accounts=[],
        recent_blockhash=Hash.from_string(recent_blockhash),
    )
    tip_tx = VersionedTransaction(tip_msg, [keypair])
    tip_tx_b58 = _to_base58(bytes(tip_tx))
    swap_tx_b58 = _to_base58(swap_tx_bytes)

    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "sendBundle",
        "params": [[swap_tx_b58, tip_tx_b58]],
    }

    async with aiohttp.ClientSession(connector=rpc_client.get_http_connector(), connector_owner=False) as session:
        async with session.post(config.JITO_BLOCK_ENGINE_URL, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise JitoError(f"sendBundle a échoué ({resp.status}): {text[:200]}")
            data = await resp.json()
            if "error" in data:
                raise JitoError(f"sendBundle erreur : {data['error']}")
            bundle_id = data.get("result")
            log.info(f"📦 Bundle Jito envoyé : {bundle_id} (tip {tip_lamports/1_000_000_000:.5f} SOL sur {tip_account})")
            return bundle_id


def _to_base58(raw_bytes: bytes) -> str:
    import base58
    return base58.b58encode(raw_bytes).decode("utf-8")
