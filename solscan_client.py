"""
════════════════════════════════════════════════════════════════
SOLSCAN CLIENT — Alternative à Helius pour les transferts à montant fixe
════════════════════════════════════════════════════════════════
Construit suite à des rate-limits Helius répétés lors des scans intensifs
de pattern_detector.py (recherche de tous les transferts d'un montant
précis depuis une adresse d'exchange à fort volume). Le palier gratuit
Solscan (10M CU/mois, 1000 req/60s) est nettement plus généreux pour ce
cas d'usage précis.

Endpoint utilisé (confirmé via doc officielle mi-2026) :
    GET https://pro-api.solscan.io/v2.0/account/transfer
    Paramètres : address (requis), from (filtre expéditeur), token
    (adresse du token — utiliser le mint SOL pour les transferts natifs),
    page, page_size

⚠️ HONNÊTETÉ SUR CE QUI N'EST PAS CONFIRMÉ :
- Le nom exact du HEADER d'authentification n'a pas pu être confirmé avec
  certitude absolue via la documentation publique (qui dit juste "inclus
  ta clé dans le header" sans préciser lequel). "token" est utilisé ici
  comme convention la plus courante pour Solscan — si ton premier test
  renvoie une erreur 401, c'est probablement le bon endroit à corriger.
- Je n'ai jamais pu tester un vrai appel réseau depuis mon environnement
  (accès réseau sandbox restreint) — premier vrai test chez toi, comme
  pour Jupiter/Jito/Mobula.
"""

import logging

import aiohttp

import config
import rpc_client

log = logging.getLogger("solscan_client")

# Adresse standard du wrapped SOL sur Solana (déjà utilisée ailleurs dans
# le projet via config.SOL_MINT — réutilisée ici pour cohérence plutôt que
# de recopier une valeur qui pourrait différer).


async def find_fixed_amount_transfers(exchange_address: str, target_amount_sol: float,
                                       tolerance: float = None, max_pages: int = 20,
                                       page_size: int = 100) -> list:
    """
    Équivalent Solscan de pattern_detector.find_fixed_amount_recipients() —
    cherche les transferts SOL sortants d'une adresse dont le montant tombe
    dans [target_amount_sol - tolerance, target_amount_sol + tolerance].

    AMÉLIORÉ après consultation directe de la doc officielle (page complète
    récupérée, pas juste des extraits de recherche) : utilise le filtrage
    CÔTÉ SERVEUR de Solscan (paramètres amount[] et flow=out) plutôt que de
    tout récupérer puis filtrer nous-mêmes — confirmé dans la doc officielle,
    beaucoup plus efficace en requêtes.

    Retourne une liste de dicts : {"recipient": str, "amount_sol": float, "signature": str}
    ou [] si pas de clé configurée / erreur réseau (jamais bloquant — voir
    pattern_detector.py qui peut retomber sur Helius dans ce cas).
    """
    if not config.SOLSCAN_API_KEY:
        return []

    tolerance = tolerance if tolerance is not None else config.FIXED_AMOUNT_TOLERANCE_SOL
    lo_sol = target_amount_sol - tolerance
    hi_sol = target_amount_sol + tolerance
    # Solscan attend le montant dans l'unité la plus petite (comme les
    # lamports) — confirmé par le champ de réponse "amount" documenté comme
    # "diviser par 10^token_decimals pour le montant réel", 9 décimales pour SOL.
    lo_raw = int(lo_sol * 1_000_000_000)
    hi_raw = int(hi_sol * 1_000_000_000)

    headers = {"token": config.SOLSCAN_API_KEY}
    matches = []

    for page in range(1, max_pages + 1):
        params = {
            "address": exchange_address,
            "token": config.SOL_MINT,
            "flow": "out",           # ne récupère que les sorties — confirmé par la doc
            "amount[]": [lo_raw, hi_raw],  # filtrage serveur — confirmé par la doc
            "page": page,
            "page_size": page_size,
        }
        try:
            async with aiohttp.ClientSession(connector=rpc_client.get_http_connector(), connector_owner=False) as session:
                async with session.get(
                    f"{config.SOLSCAN_API_BASE_URL}/account/transfer",
                    params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 401:
                        log.warning(
                            "Clé Solscan rejetée (401) — le header d'authentification "
                            "('token') n'est peut-être pas le bon nom, à vérifier."
                        )
                        return matches
                    if resp.status == 429:
                        log.warning("Rate limit Solscan atteint (429).")
                        return matches
                    if resp.status != 200:
                        log.debug(f"Solscan a répondu {resp.status} (page {page})")
                        break

                    data = await resp.json()
                    transfers = data.get("data", [])
                    if not transfers:
                        break

                    for t in transfers:
                        decimals = t.get("token_decimals", 9)
                        amount_sol = t.get("amount", 0) / (10 ** decimals)
                        matches.append({
                            "recipient": t.get("to_address"),
                            "amount_sol": amount_sol,
                            "signature": t.get("trans_id"),
                        })

                    if len(transfers) < page_size:
                        break  # dernière page atteinte
        except Exception as e:
            log.debug(f"Erreur Solscan (page {page}): {e}")
            break

    log.info(f"[Solscan] {len(matches)} transferts trouvés entre {lo_sol:.6f} et {hi_sol:.6f} SOL depuis {exchange_address[:8]}...")
    return matches
