"""
════════════════════════════════════════════════════════════════
FIND DEV — Outil autonome d'analyse d'un token
════════════════════════════════════════════════════════════════
À partir d'une adresse de token existant, retrouve :
1. Le wallet du dev (créateur) — en remontant à la toute première
   transaction du token
2. Le schéma de financement de ce dev (simple/mère/exchange) — réutilise
   fund_tracer.py
3. Son historique de tokens créés — réutilise wallet_history.py
4. Un backtest de rentabilité sur cet historique — réutilise backtest.py

Usage :
    python find_dev.py <adresse_du_token>

Ne fait AUCUN achat, AUCUNE modification du monitoring — outil de lecture
seule pour analyser un token avant de décider (manuellement) d'ajouter son
dev au bot via Telegram si le résultat est intéressant.
"""

import asyncio
import sys

import aiohttp

import config
import rpc_client
import fund_tracer
import wallet_history
import backtest


async def find_token_creator(token_mint: str) -> dict:
    """
    Remonte l'historique des transactions du token jusqu'à la toute première
    (sa création) et en extrait l'adresse du dev (fee payer de cette tx).

    IMPORTANT : vérifie explicitement que cette transaction est bien une
    instruction "Create" de Pump.fun (comme websocket_listener.py le fait en
    temps réel) — ne suppose pas juste "la plus ancienne = la création" sans
    contrôle. Si ce n'est pas confirmé, on le signale clairement plutôt que
    de risquer un mauvais résultat silencieux.

    Retourne {"dev_address": str, "signature": str, "verified": bool} ou
    None si introuvable. verified=False signifie que le résultat est une
    approximation moins fiable (transaction ancienne trouvée, mais son type
    exact n'a pas pu être confirmé comme "Create").
    """
    signatures = await _get_all_signatures_for_mint(token_mint)
    if not signatures:
        return None

    # Solana retourne du plus récent au plus ancien — la création est donc
    # la DERNIÈRE signature de la liste complète paginée.
    oldest_sig = signatures[-1]["signature"]

    parsed = await _fetch_parsed_transaction(oldest_sig)
    if not parsed:
        return None

    dev_address = parsed.get("feePayer")
    if not dev_address:
        return None

    # Vérification explicite du type — évite de faire confiance aveuglément
    # à "la plus ancienne transaction = la création".
    description = (parsed.get("description") or "").lower()
    tx_type = parsed.get("type", "")
    source = parsed.get("source", "")
    verified = (
        "create" in description
        or tx_type == "TOKEN_MINT"
        or source == "PUMP_FUN"
    )
    if not verified:
        print(
            f"⚠️  Attention : impossible de confirmer que la transaction {oldest_sig[:12]}... "
            f"est bien une création Pump.fun (type détecté : '{tx_type}', source : '{source}'). "
            f"Le dev retourné est une approximation, à vérifier manuellement sur Solscan."
        )


    return {"dev_address": dev_address, "signature": oldest_sig, "verified": verified}


async def _get_all_signatures_for_mint(mint: str, max_pages: int = 10) -> list:
    """
    Pagine getSignaturesForAddress pour remonter jusqu'à la création du token.

    CORRIGÉ suite à un échec réel : un batch vide était traité comme "fin de
    l'historique atteinte", alors que ça peut aussi être un échec RPC
    temporaire (rate limit après un usage intensif). Réessaie maintenant
    3 fois avec backoff avant d'abandonner une page.
    """
    all_sigs = []
    before = None

    for _ in range(max_pages):
        params = [mint, {"limit": 1000}]
        if before:
            params[1]["before"] = before

        payload = {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress", "params": params}
        batch = await _rpc_post_with_retry(payload, timeout=15)

        if not batch or not isinstance(batch, list):
            break

        all_sigs.extend(batch)
        before = batch[-1]["signature"]

        if len(batch) < 1000:
            break  # atteint le début de l'historique du token

    return all_sigs


async def _rpc_post_with_retry(payload: dict, timeout: int = 15, max_retries: int = 3):
    """Réessaie avec backoff exponentiel — évite qu'un rate-limit temporaire
    (fréquent après un usage intensif, comme un gros scan précédent) fasse
    échouer tout le pipeline dès le premier appel."""
    for attempt in range(max_retries):
        result = await rpc_client.rpc_post(payload, timeout=timeout)
        if result:
            return result
        if attempt < max_retries - 1:
            await asyncio.sleep(1.0 * (2 ** attempt))
    return None


async def _fetch_parsed_transaction(signature: str) -> dict:
    """
    Utilise l'API Enhanced Transactions de Helius. Réessaie avec backoff
    avant d'abandonner (même raison que _get_all_signatures_for_mint).

    CORRIGÉ suite à un vrai rate-limit persistant : cette fonction fait un
    appel réseau DIRECT (pas via rpc_client.rpc_post), ce qui contournait
    complètement le limiteur de débit global — les autres appels du script
    étaient bien cadencés, mais celui-ci partait à pleine vitesse en
    parallèle, dépassant quand même la limite réelle du plan Helius.
    Applique maintenant explicitement le même limiteur partagé.
    """
    if not config.HELIUS_API_KEY:
        return {}

    for attempt in range(3):
        try:
            await rpc_client._rate_limiter.wait_if_needed()
            async with aiohttp.ClientSession(connector=rpc_client.get_http_connector(), connector_owner=False) as session:
                async with session.post(
                    config.HELIUS_PARSE_TX_URL,
                    json={"transactions": [signature]},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        if attempt < 2:
                            await asyncio.sleep(1.0 * (2 ** attempt))
                            continue
                        return {}
                    data = await resp.json()
                    return data[0] if data else {}
        except Exception as e:
            if attempt < 2:
                await asyncio.sleep(1.0 * (2 ** attempt))
                continue
            print(f"⚠️  Erreur fetch transaction {signature} (après réessais): {e}")
            return {}
    return {}


async def analyze(token_mint: str):
    print(f"🔍 Analyse du token : {token_mint}\n")

    if not config.HELIUS_API_KEY:
        print("❌ Aucune clé HELIUS_API_KEY configurée dans .env — impossible de continuer.")
        return

    # ── 1. Trouver le dev ──────────────────────────────────────────
    print("── Étape 1/4 : recherche du créateur ──")
    creator_info = await find_token_creator(token_mint)
    if not creator_info:
        print("❌ Impossible de trouver le créateur (historique introuvable, token trop récent/ancien, ou erreur réseau).")
        return

    dev_address = creator_info["dev_address"]
    confidence = "✅ confirmé (instruction Create Pump.fun identifiée)" if creator_info["verified"] else "⚠️ NON confirmé (approximation — vérifie manuellement)"
    print(f"✅ Dev trouvé : {dev_address}")
    print(f"   Fiabilité : {confidence}")
    print(f"   Transaction de création : {creator_info['signature']}\n")

    # ── 2. Tracer le financement ────────────────────────────────────
    print("── Étape 2/4 : traçage du financement ──")
    trace = await fund_tracer.classify_scheme(dev_address)
    scheme = trace.get("scheme", "inconnu")
    funder = trace.get("funder_address")
    amount = trace.get("amount_sol")

    print(f"Schéma détecté : {scheme}")
    if funder:
        print(f"Financé par : {funder}")
        if amount:
            print(f"Montant du premier transfert : {amount:.4f} SOL")
        if trace.get("exchange_name"):
            print(f"Exchange identifié : {trace['exchange_name']}")
    else:
        print("Financement introuvable (historique insuffisant ou erreur réseau).")
    print()

    # ── 3. Historique des tokens créés par ce dev ───────────────────
    print("── Étape 3/4 : historique du dev ──")
    past_tokens = await wallet_history.get_created_tokens(dev_address)
    print(f"{len(past_tokens)} token(s) créé(s) précédemment par ce dev")
    for t in past_tokens[:5]:
        print(f"   - {t['token_mint']}")
    if len(past_tokens) > 5:
        print(f"   ... et {len(past_tokens) - 5} de plus")
    print()

    # ── 4. Backtest si historique suffisant ─────────────────────────
    print("── Étape 4/4 : backtest ──")
    if len(past_tokens) < 3:
        print("⏭️  Historique insuffisant (< 3 tokens) pour un backtest fiable.")
    else:
        result = await backtest.backtest_wallet(past_tokens)
        regularity = await wallet_history.check_sell_regularity(dev_address, past_tokens)

        print(f"Résultat cumulé : {result['total_result_pct']:+.1f}%")
        print(f"Ratio gain/perte : {result['ratio']:.2f} (seuil configuré : {config.BACKTEST_MIN_RATIO})")
        print(f"TP suggéré : {result['suggested_tp_pct']:.0f}%")
        print(f"Score de régularité : {regularity['regularity_score']:.2f}")
        print(f"Tokens exclus (bundle trop haut) : {result['skipped_bundle']}")
        print()
        if result["should_monitor"] and regularity["regularity_score"] >= 0.3:
            print("✅ RECOMMANDATION : ce dev correspond aux critères — envisage de l'ajouter au monitoring.")
        else:
            print("❌ RECOMMANDATION : ce dev ne correspond pas aux critères de qualité.")

    print(f"\n{'='*60}")
    print(f"Pour ajouter ce dev au bot : copie son adresse et utilise")
    print(f"➕ Add Rugger dans le menu Telegram 🎯 Ruggeurs.")
    print(f"Adresse du dev : {dev_address}")
    print(f"{'='*60}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage : python find_dev.py <adresse_du_token>")
        sys.exit(1)

    token_mint = sys.argv[1].strip()
    asyncio.run(analyze(token_mint))
