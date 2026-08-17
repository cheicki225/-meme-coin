"""
════════════════════════════════════════════════════════════════
DÉTECTION DE PATTERN — Montant fixe depuis une adresse d'exchange
════════════════════════════════════════════════════════════════
Reproduit l'étape clé de la méthode "schéma exchange" de la vidéo :
un rugger de haute qualité envoie souvent toujours le même montant
(ex: 0.992 SOL) depuis une adresse d'exchange connue vers ses
nouveaux "fresh wallets".

On surveille les sorties d'une adresse d'exchange dans un intervalle
de montant très serré, et on ajoute automatiquement chaque nouveau
destinataire au monitoring.
"""

import asyncio
import logging

import config
import rpc_client

log = logging.getLogger("pattern_detector")


async def find_fixed_amount_recipients_helius_native(exchange_address: str, target_amount_sol: float,
                                                       tolerance: float = None, limit: int = 100) -> list:
    """
    Utilise getTransfersByAddress — méthode RPC EXCLUSIVE Helius, avec
    filtrage par montant CÔTÉ SERVEUR (gte/lte), découverte après l'échec
    des tentatives Arkham/Solscan/Bitquery. Avantage majeur : réutilise la
    clé Helius déjà configurée, aucun nouveau compte/format d'auth à deviner.

    ✅ CONFIRMÉ FONCTIONNEL par un vrai test en conditions réelles (statut
    200, vraies données retournées). Format de réponse RÉEL, confirmé
    (pas une supposition) :
    {"result": {"data": [{"signature":.., "fromUserAccount":.., "toUserAccount":..,
    "amount": "<lamports en string>", "uiAmount": "<SOL en string>", ...}], "paginationToken":..}}

    Réessaie 2 fois avec backoff en cas d'échec RPC transitoire (comme les
    autres fonctions réseau du projet) avant d'abandonner.

    Retourne une liste de dicts : {"recipient": str, "amount_sol": float, "signature": str}
    ou [] si pas de clé configurée / échec persistant / méthode non supportée par ton plan.
    """
    if not config.HELIUS_API_KEY:
        return []

    tolerance = tolerance if tolerance is not None else config.FIXED_AMOUNT_TOLERANCE_SOL
    lo_lamports = int((target_amount_sol - tolerance) * 1_000_000_000)
    hi_lamports = int((target_amount_sol + tolerance) * 1_000_000_000)

    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTransfersByAddress",
        "params": [
            exchange_address,
            {
                "mint": config.SOL_MINT,
                "direction": "out",
                "filters": {"amount": {"gte": lo_lamports, "lte": hi_lamports}},
                "limit": limit,
            },
        ],
    }

    result = None
    for attempt in range(3):
        try:
            result = await rpc_client.rpc_post(payload, timeout=20)
            if result:
                break
        except Exception as e:
            log.debug(f"getTransfersByAddress (tentative {attempt+1}/3) a échoué : {e}")
        if attempt < 2:
            await asyncio.sleep(1.0 * (2 ** attempt))

    if not result:
        log.warning("getTransfersByAddress a échoué après 3 tentatives — repli sur les méthodes suivantes.")
        return []

    # Format RÉEL confirmé par test : result = {"data": [...], "paginationToken": ...}
    if isinstance(result, list):
        transfers = result
    elif isinstance(result, dict):
        transfers = result.get("data") or result.get("transfers") or []
    else:
        transfers = []

    if transfers:
        log.debug(f"[getTransfersByAddress] {len(transfers)} transfert(s) reçu(s), exemple : {transfers[0]}")

    matches = []
    for t in transfers:
        recipient = t.get("toUserAccount")
        sender = t.get("fromUserAccount")
        signature = t.get("signature")
        if not recipient or not signature:
            continue
        # Sécurité supplémentaire : le filtre "direction":"out" devrait déjà
        # garantir ça côté serveur, mais on revérifie nous-mêmes par prudence.
        if sender and sender != exchange_address:
            continue

        # "amount" et "uiAmount" sont des CHAÎNES dans la vraie réponse Helius,
        # pas des nombres — confirmé par test réel (ex: "1526296280", "1.52629628").
        ui_amount = t.get("uiAmount")
        if ui_amount is not None:
            try:
                amount_sol = float(ui_amount)
            except (TypeError, ValueError):
                continue
        else:
            try:
                amount_sol = float(t.get("amount", 0)) / 1_000_000_000
            except (TypeError, ValueError):
                continue

        matches.append({
            "recipient": recipient,
            "amount_sol": amount_sol,
            "signature": signature,
        })

    log.info(f"[Helius natif] {len(matches)} transferts trouvés entre {target_amount_sol - tolerance:.6f} et {target_amount_sol + tolerance:.6f} SOL depuis {exchange_address[:8]}...")
    return matches


async def find_fixed_amount_recipients(exchange_address: str, target_amount_sol: float,
                                        tolerance: float = None, max_pages: int = 20,
                                        page_size: int = 1000, on_progress=None,
                                        concurrency: int = 5) -> list:
    """
    Cherche, dans l'historique d'une adresse d'exchange, tous les transferts
    sortants dont le montant tombe dans [target_amount_sol - tolerance,
    target_amount_sol + tolerance].

    CORRIGÉ suite à un test réel : l'ancienne version ne scannait que 100
    signatures par défaut. Une adresse intermédiaire d'exchange à fort volume
    (type "Binance 2") fait des transactions quasiment chaque seconde — 100
    signatures ne couvrent que quelques minutes d'activité, largement
    insuffisant pour retrouver un transfert qui peut dater d'heures/jours.
    Pagine maintenant sur bien plus de signatures (jusqu'à max_pages*page_size,
    20 000 par défaut) via le curseur `before`, comme wallet_history.py.

    ACCÉLÉRÉ suite à un test réel (20 000 signatures en séquentiel = très
    lent) : vérifie maintenant les transactions par lots CONCURRENTS
    (asyncio.gather). concurrency=5 par défaut — réduit depuis 15 initial
    suite à un vrai test qui a déclenché des rate-limits Helius répétés.

    CORRIGÉ (suite au même test) : un échec RPC (rate limit, timeout) était
    silencieusement traité comme "cette transaction n'est pas un match" —
    risque réel de rater le vrai transfert cherché sans aucun signal.
    Chaque transaction ratée fait maintenant l'objet de 3 réessais avec
    backoff exponentiel avant abandon, et le nombre d'échecs définitifs est
    compté et affiché à la fin — honnêteté sur les angles morts restants.

    Retourne une liste de dicts : {"recipient": str, "amount_sol": float, "signature": str}
    """
    tolerance = tolerance or config.FIXED_AMOUNT_TOLERANCE_SOL

    # ── Essaie getTransfersByAddress (Helius natif) en premier ──────────
    # Découvert après l'échec de Arkham/Solscan/Bitquery — filtrage serveur
    # avec la clé Helius déjà configurée, pas de nouveau compte. Jamais
    # testé en conditions réelles depuis mon environnement (voir docstring
    # de la fonction) — si ça échoue silencieusement ou retourne un format
    # inattendu, on retombe simplement sur les méthodes suivantes.
    helius_native_results = await find_fixed_amount_recipients_helius_native(
        exchange_address, target_amount_sol, tolerance=tolerance,
    )
    if helius_native_results:
        log.info(f"[Helius natif] {len(helius_native_results)} résultat(s) — pas besoin d'aller plus loin.")
        return helius_native_results

    # ── Essaie Solscan si configuré ──────────────────────────────────────
    # Palier gratuit bien plus généreux que Helius pour ce cas d'usage
    # précis (voir solscan_client.py) — évite tout le mécanisme lourd de
    # pagination/concurrence/réessai ci-dessous si ça suffit.
    if config.SOLSCAN_API_KEY:
        import solscan_client
        solscan_results = await solscan_client.find_fixed_amount_transfers(
            exchange_address, target_amount_sol, tolerance=tolerance,
        )
        if solscan_results:
            log.info(f"[Solscan] {len(solscan_results)} résultat(s) — Helius natif non sollicité.")
            return solscan_results
        log.debug("Solscan n'a rien trouvé ou a échoué — repli sur le scan complet Helius.")

    lo = target_amount_sol - tolerance
    hi = target_amount_sol + tolerance

    signatures = await _get_signatures_paginated(
        exchange_address, max_pages=max_pages, page_size=page_size, on_progress=on_progress,
    )
    matches = []
    semaphore = asyncio.Semaphore(concurrency)

    async def _check_one(sig_info):
        async with semaphore:
            tx = await _get_transaction_with_retry(sig_info["signature"])
            if tx is None:
                return ("failed", None)  # échec confirmé même après réessais — jamais vraiment vérifié
            transfer = _extract_outgoing_transfer(tx, exchange_address)
            if not transfer:
                return ("checked", None)
            if lo <= transfer["amount_sol"] <= hi:
                return ("checked", {
                    "recipient": transfer["to"],
                    "amount_sol": transfer["amount_sol"],
                    "signature": sig_info["signature"],
                })
            return ("checked", None)

    scanned = 0
    failed_count = 0
    for i in range(0, len(signatures), concurrency):
        batch = signatures[i:i + concurrency]
        results = await asyncio.gather(*[_check_one(s) for s in batch])
        for status, match in results:
            if status == "failed":
                failed_count += 1
            elif match is not None:
                matches.append(match)

        scanned += len(batch)
        if on_progress and scanned % 200 < concurrency:
            on_progress(None, f"{scanned}/{len(signatures)} transactions scannées, {len(matches)} match(s), {failed_count} échec(s)")

    if failed_count > 0:
        log.warning(
            f"⚠️  {failed_count}/{len(signatures)} transactions n'ont JAMAIS pu être vérifiées "
            f"(échec RPC même après réessais) — un match aurait pu s'y trouver sans qu'on le sache."
        )

    log.info(f"{len(matches)} transferts trouvés entre {lo:.6f} et {hi:.6f} SOL depuis {exchange_address[:8]}... (sur {len(signatures)} signatures scannées, {failed_count} échec(s) non résolu(s))")
    return matches


async def _get_transaction_with_retry(signature: str, max_retries: int = 3):
    """
    Réessaie avec backoff exponentiel (0.5s, 1s, 2s) avant d'abandonner —
    évite de traiter silencieusement un échec temporaire (rate limit 429,
    timeout ponctuel) comme "cette transaction n'est pas un match", ce qui
    aurait pu faire rater le vrai transfert recherché sans aucun signal.
    """
    for attempt in range(max_retries):
        tx = await _get_transaction(signature)
        if tx:
            return tx
        if attempt < max_retries - 1:
            await asyncio.sleep(0.5 * (2 ** attempt))
    return None


async def _get_signatures_paginated(address: str, max_pages: int = 20, page_size: int = 1000,
                                     on_progress=None) -> list:
    """Pagine via le curseur `before` — voir wallet_history.get_all_signatures_paginated
    pour la même logique, dupliquée ici pour éviter une dépendance circulaire entre modules."""
    all_sigs = []
    before = None

    for page in range(max_pages):
        params = [address, {"limit": page_size}]
        if before:
            params[1]["before"] = before

        payload = {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress", "params": params}
        batch = await rpc_client.rpc_post(payload, timeout=20)

        if not batch or not isinstance(batch, list):
            break

        all_sigs.extend(batch)
        before = batch[-1]["signature"]

        if on_progress:
            on_progress(page + 1, len(all_sigs))

        if len(batch) < page_size:
            break

    return all_sigs


async def find_multi_range_recipients(exchange_address: str, ranges: list, max_results: int = 100) -> list:
    """
    Version multi-plages de find_fixed_amount_recipients — accepte jusqu'à
    plusieurs plages [{"min": x, "max": y}, ...] simultanément (Transfer
    Ranges du menu Protection, jusqu'à 3 plages configurables). Un transfert
    est retenu s'il tombe dans N'IMPORTE LAQUELLE des plages fournies.

    Retourne une liste de dicts : {"recipient": str, "amount_sol": float,
    "signature": str, "matched_range": dict}
    """
    if not ranges:
        return []

    signatures = await _get_signatures(exchange_address, limit=max_results)
    matches = []

    for sig_info in signatures:
        tx = await _get_transaction(sig_info["signature"])
        if not tx:
            continue

        transfer = _extract_outgoing_transfer(tx, exchange_address)
        if not transfer:
            continue

        for r in ranges:
            if r["min"] <= transfer["amount_sol"] <= r["max"]:
                matches.append({
                    "recipient": transfer["to"],
                    "amount_sol": transfer["amount_sol"],
                    "signature": sig_info["signature"],
                    "matched_range": r,
                })
                break  # une plage suffit, pas de doublon si plusieurs matchent

    log.info(
        f"{len(matches)} transferts trouvés sur {len(ranges)} plage(s) configurée(s) "
        f"depuis {exchange_address[:8]}..."
    )
    return matches


async def find_all_outgoing_recipients(source_address: str, max_results: int = 50) -> list:
    """
    Version "schéma mère" : contrairement à find_fixed_amount_recipients, ne filtre
    pas par montant — retourne TOUS les destinataires des transferts SOL sortants
    récents. Utile pour une adresse "mère" qui finance ses fresh wallets avec des
    montants variables (contrairement au schéma exchange qui utilise un montant fixe).

    Retourne une liste de dicts : {"recipient": str, "amount_sol": float, "signature": str}
    """
    signatures = await _get_signatures(source_address, limit=max_results)
    results = []

    for sig_info in signatures:
        tx = await _get_transaction(sig_info["signature"])
        if not tx:
            continue

        transfer = _extract_outgoing_transfer(tx, source_address)
        if not transfer:
            continue

        results.append({
            "recipient": transfer["to"],
            "amount_sol": transfer["amount_sol"],
            "signature": sig_info["signature"],
        })

    return results


async def _get_signatures(address: str, limit: int = 100) -> list:
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [address, {"limit": limit}],
    }
    result = await rpc_client.rpc_post(payload, timeout=15)
    return result if isinstance(result, list) else []


async def _get_transaction(signature: str) -> dict:
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTransaction",
        "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    }
    return await rpc_client.rpc_post(payload, timeout=12)


def _extract_outgoing_transfer(tx: dict, source_address: str) -> dict:
    """Trouve un transfert SOL sortant depuis source_address dans cette transaction."""
    try:
        message = tx["transaction"]["message"]
        account_keys = [k.get("pubkey", k) if isinstance(k, dict) else k for k in message["accountKeys"]]

        pre_balances = tx["meta"]["preBalances"]
        post_balances = tx["meta"]["postBalances"]

        source_idx = account_keys.index(source_address) if source_address in account_keys else None
        if source_idx is None:
            return None

        decrease = pre_balances[source_idx] - post_balances[source_idx]
        if decrease <= 0:
            return None

        # Le destinataire probable = compte dont le solde a le plus augmenté
        max_increase_idx, max_increase = None, 0
        for i, (pre, post) in enumerate(zip(pre_balances, post_balances)):
            if i == source_idx:
                continue
            increase = post - pre
            if increase > max_increase:
                max_increase = increase
                max_increase_idx = i

        if max_increase_idx is None:
            return None

        return {
            "to": account_keys[max_increase_idx],
            "amount_sol": max_increase / 1_000_000_000,
        }
    except (KeyError, IndexError, ValueError) as e:
        log.debug(f"Erreur extraction transfert sortant: {e}")
        return None
