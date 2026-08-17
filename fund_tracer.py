"""
════════════════════════════════════════════════════════════════
TRAÇAGE DES FONDS — Identifier le "funder" d'un wallet dev
════════════════════════════════════════════════════════════════
Reproduit l'étape "SolScan → remonter l'adresse qui a fund le wallet"
de la vidéo. On récupère la toute première transaction reçue par une
adresse (généralement la création + premier apport de SOL) et on
détermine de quel schéma il s'agit :

- SIMPLE   : wallet A → wallet B (transfert direct entre 2 wallets perso)
- MERE     : passage par une adresse centrale (le rugger a un wallet "coffre")
- EXCHANGE : les fonds viennent d'une adresse connue de Binance/Bybit/etc.

Le schéma EXCHANGE est le plus intéressant (rugger de "haute qualité"
selon la vidéo) car il permet la détection par montant fixe.
"""

import logging

import config
import rpc_client

log = logging.getLogger("fund_tracer")


async def is_fresh_wallet(wallet_address: str, max_prior_signatures: int = 1) -> bool:
    """
    Vérifie si un wallet est "neuf" (Fresh Wallet) — c'est-à-dire qu'il n'a
    quasiment aucune activité on-chain avant le transfert qui vient de le
    financer. max_prior_signatures=1 signifie qu'on tolère la transaction de
    financement elle-même, mais rien d'autre avant.

    Retourne True si le wallet est fresh, False sinon. En cas d'erreur réseau,
    retourne False par prudence (mieux vaut rater un ajout que d'accepter un
    faux positif sur cette protection).
    """
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [wallet_address, {"limit": max_prior_signatures + 5}],
    }
    try:
        import rpc_client
        signatures = await rpc_client.rpc_post(payload, timeout=10)
        if not isinstance(signatures, list):
            return False
        return len(signatures) <= max_prior_signatures
    except Exception:
        return False


async def get_first_funder(wallet_address: str) -> dict:
    """
    Trouve la toute première transaction entrante d'un wallet (càd celle
    qui l'a "fund" au tout début) — et le montant associé.

    AMÉLIORÉ (après découverte de getTransfersByAddress pour l'étape 3) :
    essaie d'abord cette même méthode RPC native Helius, direction "in",
    qui répond en UNE requête au lieu de paginer potentiellement des
    dizaines de signatures + vérifier chaque transaction une par une.
    Repli automatique sur l'ancienne méthode (scan complet) si ça échoue.

    Retourne : {
        "funder_address": str | None,
        "amount_sol": float,
        "signature": str,
        "scheme": "simple" | "mere" | "exchange" | "inconnu",
        "exchange_name": str | None,
    }
    """
    # ── Essaie getTransfersByAddress en premier (rapide, 1 requête) ──────
    native_result = await _get_first_funder_via_helius_native(wallet_address)
    if native_result:
        return await _finish_funder_classification(native_result["from"], native_result["amount_sol"], native_result["signature"])

    signatures = await _get_all_signatures(wallet_address)
    if not signatures:
        return {"funder_address": None, "scheme": "inconnu"}

    # La première transaction chronologiquement est la dernière de la liste
    # (Solana retourne du plus récent au plus ancien)
    oldest_sig = signatures[-1]["signature"]

    tx = await _get_transaction(oldest_sig)
    if not tx:
        return {"funder_address": None, "scheme": "inconnu"}

    funder_info = _extract_transfer_source(tx, wallet_address)
    if not funder_info:
        return {"funder_address": None, "scheme": "inconnu"}

    funder_address = funder_info["from"]
    amount_sol = funder_info["amount_sol"]
    return await _finish_funder_classification(funder_address, amount_sol, oldest_sig)


async def _get_first_funder_via_helius_native(wallet_address: str) -> dict:
    """
    Essaie getTransfersByAddress (RPC natif Helius) direction "in", trié
    pour trouver la plus ancienne transaction entrante — 1 seule requête
    au lieu de paginer + vérifier des dizaines de transactions une par une.
    Retourne {"from": str, "amount_sol": float, "signature": str} ou None
    si la méthode échoue (repli automatique sur l'ancienne méthode).
    """
    if not config.HELIUS_API_KEY:
        return None

    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTransfersByAddress",
        "params": [wallet_address, {"direction": "in", "limit": 100}],
    }
    try:
        result = await rpc_client.rpc_post(payload, timeout=20)
    except Exception:
        return None

    if not result:
        return None

    if isinstance(result, list):
        transfers = result
    elif isinstance(result, dict):
        transfers = result.get("transfers") or result.get("data") or result.get("result") or []
    else:
        return None

    if not transfers:
        return None

    # Prend la plus ancienne (blockTime le plus petit) — la réponse n'est
    # pas garantie triée dans cet ordre selon la doc, donc on trie nous-mêmes
    # par sécurité plutôt que de supposer l'ordre.
    try:
        oldest = min(transfers, key=lambda t: t.get("blockTime", float("inf")))
    except Exception:
        oldest = transfers[-1]  # repli si blockTime absent

    sender = oldest.get("fromUserAccount") or oldest.get("from") or oldest.get("sender")
    signature = oldest.get("signature") or oldest.get("txHash")
    amount_raw = oldest.get("amount", 0)
    if not sender:
        return None

    try:
        amount_sol = float(amount_raw) / 1_000_000_000
    except (ValueError, TypeError):
        return None

    return {"from": sender, "amount_sol": amount_sol, "signature": signature}


async def _finish_funder_classification(funder_address: str, amount_sol: float, oldest_sig: str) -> dict:
    """Logique de classification partagée (exchange/mère/simple, Mobula) —
    utilisée que le financeur ait été trouvé via getTransfersByAddress ou
    via l'ancienne méthode de scan complet."""
    exchange_name = _match_known_exchange(funder_address)

    # Si notre petite liste locale ne connaît pas l'adresse, on demande à
    # Mobula (si une clé est configurée, gratuite) — enrichit la détection
    # au-delà des quelques adresses qu'on maintient nous-mêmes à la main,
    # et récupère en bonus les labels comportementaux du wallet lui-même
    # (sniper/insider/bundler/freshTrader — voir mobula_client.py).
    mobula_verified = False
    mobula_labels = []
    if not exchange_name:
        try:
            import mobula_client
            mobula_result = await mobula_client.get_wallet_labels(funder_address)
            if mobula_result:
                mobula_labels = mobula_result.get("labels", [])
                # IMPORTANT : on ne se base QUE sur is_exchange (est-ce que cette
                # adresse ELLE-MÊME est un exchange connu). funder_identity décrit
                # qui a financé CETTE adresse (un niveau plus loin) — l'utiliser
                # ici serait une erreur de logique (ex: "Solana" identifié comme
                # financeur du financeur ne veut pas dire que l'adresse elle-même
                # est un exchange). Confirmé par un vrai test : sans ce garde-fou,
                # le bot affichait "Exchange identifié : Solana" à tort.
                if mobula_result["is_exchange"]:
                    exchange_name = mobula_result["entity_name"]
                    mobula_verified = True
        except Exception as e:
            log.debug(f"Erreur vérification Mobula pour {funder_address[:8]}...: {e}")

    # Repli par COMPORTEMENT (volume de transactions) — ni notre liste locale
    # ni Mobula ne connaissent TOUS les exchanges (des dizaines d'adresses
    # intermédiaires différentes par exchange, impossible à lister
    # exhaustivement). Confirmé par un vrai cas réel : une adresse Binance
    # authentique (visible sur Solscan) n'était reconnue ni par notre liste,
    # ni par Mobula, et le schéma tombait à tort sur "simple". Une adresse à
    # très fort volume (des milliers de transactions) est presque
    # certainement un exchange ou une adresse pont, peu importe si on
    # connaît son nom — on classe donc quand même comme "exchange", avec un
    # nom générique plutôt que de rater complètement la classification.
    detected_by_volume = False
    if not exchange_name:
        try:
            is_high_volume = await _is_high_volume_address(funder_address)
            if is_high_volume:
                exchange_name = "Exchange non identifié (volume élevé)"
                detected_by_volume = True
        except Exception as e:
            log.debug(f"Erreur vérification volume pour {funder_address[:8]}...: {e}")

    scheme = "exchange" if exchange_name else "simple"

    if detected_by_volume:
        exchange_source = "volume"
    elif mobula_verified:
        exchange_source = "mobula"
    elif exchange_name:
        exchange_source = "liste locale"
    else:
        exchange_source = None

    return {
        "funder_address": funder_address,
        "amount_sol": amount_sol,
        "signature": oldest_sig,
        "scheme": scheme,
        "exchange_name": exchange_name,
        "exchange_source": exchange_source,
        "funder_behavior_labels": mobula_labels,  # ex: ["sniper", "bundler"] si Mobula en connaît
    }


async def _is_high_volume_address(address: str, threshold: int = 1000) -> bool:
    """
    Vérifie si une adresse a un volume de transactions anormalement élevé
    (signe d'un exchange/adresse pont, pas un wallet personnel normal) —
    SANS avoir besoin de compter tout l'historique. Demande une seule page
    de `threshold` signatures : si la page est PLEINE (il y en a au moins
    `threshold`), c'est un signal suffisant, pas besoin de paginer plus loin
    juste pour cette vérification légère.
    """
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [address, {"limit": threshold}],
    }
    result = await rpc_client.rpc_post(payload, timeout=15)
    if not isinstance(result, list):
        return False
    return len(result) >= threshold


async def classify_scheme(dev_address: str) -> dict:
    """
    Version enrichie : détermine si le schéma est SIMPLE, MERE ou EXCHANGE.
    Pour distinguer MERE de SIMPLE, on vérifie si le funder lui-même a reçu
    ses fonds de plusieurs sources différentes ET a financé plusieurs wallets
    différents dans le passé (signe d'une adresse "coffre" centrale).
    """
    first_hop = await get_first_funder(dev_address)
    if first_hop["scheme"] == "exchange" or not first_hop["funder_address"]:
        return first_hop

    funder = first_hop["funder_address"]
    outgoing_targets = await _count_distinct_outgoing_recipients(funder)

    if outgoing_targets >= 3:
        first_hop["scheme"] = "mere"
    else:
        first_hop["scheme"] = "simple"

    return first_hop


def _match_known_exchange(address: str) -> str:
    for name, addresses in config.KNOWN_EXCHANGE_ADDRESSES.items():
        if address in addresses:
            return name
    return None


async def _get_all_signatures(wallet_address: str, max_pages: int = 5) -> list:
    """Pagine getSignaturesForAddress pour remonter le plus loin possible."""
    all_sigs = []
    before = None

    for _ in range(max_pages):
        params = [wallet_address, {"limit": 1000}]
        if before:
            params[1]["before"] = before

        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": params,
        }
        batch = await rpc_client.rpc_post(payload, timeout=15)
        if not isinstance(batch, list) or not batch:
            break

        all_sigs.extend(batch)
        before = batch[-1]["signature"]

        if len(batch) < 1000:
            break  # on a atteint le début de l'historique

    return all_sigs


async def _get_transaction(signature: str) -> dict:
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTransaction",
        "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    }
    return await rpc_client.rpc_post(payload, timeout=12)


def _extract_transfer_source(tx: dict, target_wallet: str) -> dict:
    """Trouve, dans une transaction, le transfert SOL entrant vers target_wallet."""
    try:
        message = tx["transaction"]["message"]
        account_keys = [k.get("pubkey", k) if isinstance(k, dict) else k for k in message["accountKeys"]]

        pre_balances = tx["meta"]["preBalances"]
        post_balances = tx["meta"]["postBalances"]

        target_idx = account_keys.index(target_wallet) if target_wallet in account_keys else None
        if target_idx is None:
            return None

        delta_lamports = post_balances[target_idx] - pre_balances[target_idx]
        if delta_lamports <= 0:
            return None

        # Le "from" probable = compte dont le solde a le plus baissé (hors frais)
        max_decrease_idx, max_decrease = None, 0
        for i, (pre, post) in enumerate(zip(pre_balances, post_balances)):
            if i == target_idx:
                continue
            decrease = pre - post
            if decrease > max_decrease:
                max_decrease = decrease
                max_decrease_idx = i

        if max_decrease_idx is None:
            return None

        return {
            "from": account_keys[max_decrease_idx],
            "amount_sol": delta_lamports / 1_000_000_000,
        }
    except (KeyError, IndexError, ValueError) as e:
        log.debug(f"Erreur extraction transfert: {e}")
        return None


async def _count_distinct_outgoing_recipients(address: str, sample_size: int = 50) -> int:
    """Estime le nombre de destinataires distincts financés par cette adresse récemment."""
    signatures = await _get_all_signatures(address, max_pages=1)
    signatures = signatures[:sample_size]

    recipients = set()
    for sig_info in signatures:
        tx = await _get_transaction(sig_info["signature"])
        if not tx:
            continue
        info = _extract_transfer_source(tx, address)
        # Ici on cherche plutôt les sorties, logique simplifiée : à raffiner
        # avec une vraie analyse des postBalances pour chaque destinataire.
    return len(recipients)
