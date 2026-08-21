"""
════════════════════════════════════════════════════════════════
GMGN CLIENT — Wrapper Python pour l'OpenAPI GMGN (lecture seule)
════════════════════════════════════════════════════════════════
AJOUTÉ suite à une demande explicite. URL de base, en-têtes et format
d'authentification confirmés DIRECTEMENT depuis le code source du package
npm officiel gmgn-cli (dist/client/OpenApiClient.js, dist/client/signer.js,
dist/commands/portfolio.js) — pas devinés ni improvisés.

Volontairement limité aux endpoints en authentification "Exist" (clé API
seule : en-tête X-APIKEY + query params timestamp/client_id, AUCUNE
signature cryptographique requise) :
  - wallet_stats     (portfolio stats — PnL, win rate, distribution)
  - wallet_activity  (portfolio activity — historique des trades)
  - created_tokens   (portfolio created-tokens — statut dev)

get_wallet_holdings n'est PAS implémenté ici : cet endpoint GMGN utilise
l'authentification "Signed" (X-Signature calculée avec GMGN_PRIVATE_KEY,
Ed25519 ou RSA-SHA256) — une clé qu'on a délibérément choisi de garder
UNIQUEMENT en local (jamais sur Railway, voir discussion avec Cheicki du
19 août). Si besoin plus tard, il faudrait reproduire cette signature et
déployer la clé privée sur Railway — un vrai changement de posture de
sécurité, pas juste un endpoint de plus.

Config requise (variable d'environnement RAILWAY, PAS le fichier .env
local de gmgn-cli — deux environnements séparés, voir discussion) :
GMGN_API_KEY. Si absente, toutes les fonctions retournent {} silencieusement
(comme mobula_client.py) plutôt que de faire planter l'appelant.

Rate limit GMGN par défaut : 1 requête/seconde — pas de limiteur dédié
ajouté ici pour l'instant (usage prévu : actions ponctuelles déclenchées
par "Analyse de wallet"/"Analyse de dev", pas une boucle temps réel).
"""

import time
import uuid
import logging

import aiohttp

import config
import rpc_client

log = logging.getLogger("gmgn_client")

GMGN_BASE_URL = "https://openapi.gmgn.ai"


def _auth_query() -> dict:
    """Reproduit buildAuthQuery() de gmgn-cli (signer.js), confirmé dans le
    code source réel : timestamp Unix en secondes + UUID v4 frais à chaque
    requête. Aucun hash/HMAC en mode "Exist" — uniquement utilisé par le
    mode "Signed" (holdings, swap), pas par les endpoints de ce module."""
    return {"timestamp": int(time.time()), "client_id": str(uuid.uuid4())}


async def _get(sub_path: str, params: dict = None) -> dict:
    if not config.GMGN_API_KEY:
        return {}

    query = _auth_query()
    if params:
        query.update(params)

    headers = {
        "X-APIKEY": config.GMGN_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": "flach-coin-bot/1.0",
    }

    try:
        async with aiohttp.ClientSession(connector=rpc_client.get_http_connector(), connector_owner=False) as session:
            async with session.get(
                f"{GMGN_BASE_URL}{sub_path}", headers=headers, params=query,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    log.warning(f"⚠️ GMGN — {sub_path} a répondu {resp.status}")
                    return {}
                data = await resp.json()
                # Certaines réponses GMGN enveloppent dans {code, message, data} —
                # gère les deux cas plutôt que de supposer l'un ou l'autre.
                return data.get("data", data) if isinstance(data, dict) else data
    except Exception as e:
        log.warning(f"⚠️ GMGN — erreur requête {sub_path}: {e}")
        return {}


async def get_wallet_stats(wallet_address: str, chain: str = "sol") -> dict:
    """
    Stats globales GMGN pour ce wallet : PnL réalisé, win rate, distribution
    des multiples de gain (pnl_stat.pnl_gt_5x_num etc.), infos de
    financement (common.fund_from/fund_from_address — équivalent GMGN de
    notre fund_tracer.get_first_funder), tags de classification
    (common.tags — sniper/bundler/smart money quand GMGN les connaît).

    Retourne {} si GMGN n'a aucune donnée sur ce wallet (pas une erreur en
    soi — arrive pour des wallets peu actifs ou trop récents pour leur
    indexation), ou si GMGN_API_KEY n'est pas configurée.
    """
    return await _get("/v1/user/wallet_stats", {"chain": chain, "wallet_address": wallet_address})


async def get_wallet_activity(wallet_address: str, chain: str = "sol", limit: int = 20) -> dict:
    """Historique des trades (achats/ventes) de ce wallet selon GMGN —
    complémentaire à wallet_history.get_recent_buys (source on-chain
    directe), utile pour comparer les deux sources si besoin."""
    return await _get("/v1/user/wallet_activity", {"chain": chain, "wallet_address": wallet_address, "limit": limit})


async def get_created_tokens(wallet_address: str, chain: str = "sol") -> dict:
    """Tokens créés par ce wallet selon GMGN — inclut normalement l'ATH
    market cap et le statut de migration par token (données que notre
    propre wallet_history.get_created_tokens ne calcule pas nativement,
    seulement via un appel séparé à get_detailed_trade_info)."""
    return await _get("/v1/user/created_tokens", {"chain": chain, "wallet_address": wallet_address})


async def get_wallet_profits(wallet_address: str, chain: str = "sol") -> dict:
    """
    AJOUTÉ (demande explicite, 19 août) : profit agrégé déjà calculé côté
    serveur GMGN — realized_profit, unrealized_profit, total_profit,
    total_cost, nombre d'achats/ventes sur la période (7 jours par défaut
    côté GMGN). Contrairement à wallet_stats (win rate + distribution des
    multiples), celui-ci donne le $ réalisé — utilisé ensemble dans le
    résumé rapide de "Analyse de wallet" pour éviter la reconstruction
    on-chain coûteuse par défaut (voir telegram_bot.analyze_wallet_inline).

    Confirmé "Exist auth" (clé API seule, pas de clé privée) malgré la
    méthode POST (contrairement aux autres endpoints GET de ce module) —
    vérifié directement dans le code source de gmgn-cli, pas supposé.

    Retourne {"list": [...]} — un objet par wallet interrogé (supporte les
    requêtes par lot côté GMGN, mais ce client n'interroge qu'un wallet à
    la fois pour l'instant).
    """
    if not config.GMGN_API_KEY:
        return {}

    query = _auth_query()
    headers = {
        "X-APIKEY": config.GMGN_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": "flach-coin-bot/1.0",
    }
    body = {"chain": chain, "wallet_addresses": [wallet_address]}

    try:
        async with aiohttp.ClientSession(connector=rpc_client.get_http_connector(), connector_owner=False) as session:
            async with session.post(
                f"{GMGN_BASE_URL}/v1/user/wallet_profits", headers=headers, params=query, json=body,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    log.warning(f"⚠️ GMGN — wallet_profits a répondu {resp.status}")
                    return {}
                data = await resp.json()
                return data.get("data", data) if isinstance(data, dict) else data
    except Exception as e:
        log.warning(f"⚠️ GMGN — erreur requête wallet_profits: {e}")
        return {}
