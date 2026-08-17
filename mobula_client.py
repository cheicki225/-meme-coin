"""
════════════════════════════════════════════════════════════════
MOBULA CLIENT — Identification d'adresses (gratuit, remplace Arkham)
════════════════════════════════════════════════════════════════
Contrairement à Arkham (accès sur demande, jamais confirmé gratuit), Mobula
propose une clé API gratuite en libre-service immédiat via admin.mobula.io.

Un seul appel donne BEAUCOUP plus que ce qu'on avait avec Arkham :
- Labels comportementaux : "sniper", "insider", "bundler", "freshTrader",
  "proTrader", "smartTrader" — directement pertinents pour évaluer un dev
  ou un wallet copytrade, pas juste "est-ce un exchange"
- fundingInfo : qui a financé ce wallet, avec quel montant, quelle devise,
  et l'identité du financeur si connue — recoupe/enrichit notre propre
  fund_tracer.py plutôt que de le remplacer

Endpoint vérifié via la doc officielle (docs.mobula.io, mi-2026) :
    POST https://api.mobula.io/api/1/wallet/labels
    Header : Authorization: <ta_clé>
    Body   : {"walletAddresses": ["addr", ...], "tokenAddress": "optionnel"}
"""

import logging

import aiohttp

import config
import rpc_client

log = logging.getLogger("mobula_client")

MOBULA_LABELS_URL = "https://api.mobula.io/api/1/wallet/labels"


async def get_wallet_labels(address: str, token_address: str = None) -> dict:
    """
    Interroge Mobula pour un wallet donné. Retourne :
    {
        "labels": list[str],              # ex: ["sniper", "bundler"]
        "entity_name": str | None,        # ex: "Binance" si identifié
        "entity_type": str | None,
        "is_exchange": bool,
        "funder_address": str | None,     # fundingInfo.from
        "funder_amount": float | None,
        "funder_identity": str | None,    # fromWalletMetadata.entityName si connu
    }
    ou None si pas de clé configurée / erreur réseau / adresse inconnue de Mobula.
    Jamais bloquant pour le reste du bot dans tous ces cas.
    """
    if not config.MOBULA_API_KEY:
        return None

    body = {"walletAddresses": [address]}
    if token_address:
        body["tokenAddress"] = token_address

    headers = {"Authorization": config.MOBULA_API_KEY, "Content-Type": "application/json"}

    try:
        async with aiohttp.ClientSession(connector=rpc_client.get_http_connector(), connector_owner=False) as session:
            async with session.post(MOBULA_LABELS_URL, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 401:
                    log.warning("Clé Mobula invalide.")
                    return None
                if resp.status not in (200, 201):
                    log.debug(f"Mobula a répondu {resp.status} pour {address[:8]}...")
                    return None

                data = await resp.json()
                entries = data.get("data", [])
                if not entries:
                    return None

                entry = entries[0]
                labels = entry.get("labels", []) or []
                metadata = entry.get("walletMetadata") or {}
                entity_name = metadata.get("entityName")
                entity_type = metadata.get("entityType")
                is_exchange = bool(entity_type) and entity_type.lower() in ("cex", "exchange", "centralized-exchange")

                funding = entry.get("fundingInfo") or {}
                funder_metadata = funding.get("fromWalletMetadata") or {}

                return {
                    "labels": labels,
                    "entity_name": entity_name,
                    "entity_type": entity_type,
                    "is_exchange": is_exchange,
                    "funder_address": funding.get("from"),
                    "funder_amount": funding.get("formattedAmount"),
                    "funder_identity": funder_metadata.get("entityName"),
                }
    except Exception as e:
        log.debug(f"Erreur requête Mobula pour {address[:8]}...: {e}")
        return None


async def get_deployer_tokens(wallet_address: str, blockchain: str = "solana") -> list:
    """
    Utilise l'endpoint "Wallet Deployer Tokens" de Mobula (GET /api/2/wallet/deployer)
    — retourne directement la liste des tokens déployés/créés par une adresse.

    Découvert comme alternative à Helius pour les vérifications ⭐/⚪ de
    find_dev_cluster.py — utilise un quota SÉPARÉ de celui de Helius, ce qui
    aide à répartir la charge quand Helius est proche de sa limite de débit.

    ⚠️ HONNÊTETÉ : le format exact des paramètres et de la réponse n'a pas pu
    être confirmé via la documentation publique (confirmé seulement : le
    chemin de l'endpoint et le header d'authentification "Authorization").
    Parsing défensif ci-dessous, comme pour les autres intégrations — premier
    vrai test chez toi pour valider/corriger le format exact.

    Retourne une liste de dicts : {"token_mint": str, ...données brutes...}
    ou [] si pas de clé configurée / erreur / format inattendu.
    """
    if not config.MOBULA_API_KEY:
        return []

    headers = {"Authorization": config.MOBULA_API_KEY}
    params = {"wallet": wallet_address, "blockchain": blockchain}

    try:
        async with aiohttp.ClientSession(connector=rpc_client.get_http_connector(), connector_owner=False) as session:
            async with session.get(
                "https://api.mobula.io/api/2/wallet/deployer",
                params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 401:
                    log.warning("Clé Mobula rejetée (401) pour wallet/deployer.")
                    return []
                if resp.status not in (200, 201):
                    log.debug(f"Mobula wallet/deployer a répondu {resp.status} pour {wallet_address[:8]}...")
                    return []

                data = await resp.json()

                # Format CONFIRMÉ par un vrai test (14 août) : liste d'objets
                # style "wallet position", où chaque item a une clé "token"
                # contenant les vraies métadonnées (address, deployer,
                # createdAt, deployerTokensCount...).
                if isinstance(data, list):
                    raw_tokens = data
                elif isinstance(data, dict):
                    raw_tokens = data.get("data") or data.get("tokens") or data.get("deployedTokens") or []
                else:
                    return []

                results = []
                for t in raw_tokens:
                    if isinstance(t, str):
                        results.append({"token_mint": t})
                        continue
                    if not isinstance(t, dict):
                        continue

                    # Structure confirmée : t["token"] contient les vraies
                    # métadonnées. Repli sur t lui-même si jamais un format
                    # différent apparaît un jour (adresses directement au
                    # premier niveau, sans wrapper "token").
                    token_obj = t.get("token") if isinstance(t.get("token"), dict) else t
                    mint = token_obj.get("address")
                    if not isinstance(mint, str) or not mint:
                        continue

                    block_time = None
                    created_at_iso = token_obj.get("createdAt")
                    if created_at_iso:
                        try:
                            from datetime import datetime
                            block_time = int(datetime.fromisoformat(created_at_iso.replace("Z", "+00:00")).timestamp())
                        except Exception:
                            pass

                    results.append({
                        "token_mint": mint,
                        "deployer": token_obj.get("deployer"),           # double vérification gratuite
                        "block_time": block_time,                        # vraie date de création
                        "deployer_tokens_count": token_obj.get("deployerTokensCount"),  # bonus utile
                    })

                if results:
                    log.debug(f"[Mobula wallet/deployer] {len(results)} token(s) — premier : {results[0]}")

                return results
    except Exception as e:
        log.debug(f"Erreur Mobula wallet/deployer pour {wallet_address[:8]}...: {e}")
        return []
