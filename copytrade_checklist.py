"""
════════════════════════════════════════════════════════════════
CHECKLIST COPY TRADING COMPLÈTE (méthode A à H)
════════════════════════════════════════════════════════════════
AJOUTÉ suite à une demande explicite. Avant ce fichier, seule l'étape H
(rentabilité) était construite, dans analyze_copytrade_candidate.py — les
7 autres étapes n'existaient nulle part dans le bot.

État de chaque étape après ce fichier :
  A  - Trouver un bon rug             : PAS ICI — nécessite un flux global
                                         de nouveaux tokens Pump.fun, pas
                                         une analyse par wallet. Voir le
                                         système de scan complet (demande
                                         séparée, sur la liste principale).
  B  - 10 premiers acheteurs           : ✅ construit — get_first_n_buyers()
  C1 - Double signature (bot externe)  : ⚠️ APPROXIMATION FAIBLE — voir
                                         l'avertissement détaillé dans
                                         check_bot_signature(). Ne peut PAS
                                         détecter un bot de façon fiable
                                         depuis les seules données on-chain
                                         publiques.
  C2 - Comparaison bundle              : ✅ construit — check_bundle_buy()
  D  - Fréquence d'achat               : ✅ construit — check_buy_frequency()
  E  - Solde jamais à zéro avant       : ⚠️ APPROXIMATION — voir
                                         check_continuous_balance(), le
                                         seuil utilisé n'est pas défini
                                         précisément dans la doc d'origine
  F  - Win rate sur 7 jours            : ✅ construit — get_win_rate_7d()
  G  - Cohérence des points d'entrée   : ✅ construit — check_entry_consistency()
  H  - Test de rentabilité final       : ✅ déjà construit ailleurs —
                                         analyze_copytrade_candidate.py

run_full_checklist() agrège B à H (pas A, voir ci-dessus) en un seul appel.
"""

import asyncio
import statistics
import time as time_module

import config
import wallet_history
import backtest

# ════════════════════════════════════════════════════════════════
# ÉTAPE B — 10 premiers acheteurs après le dev
# ════════════════════════════════════════════════════════════════

async def get_first_n_buyers(token_mint: str, n: int = 10, dev_address: str = None) -> list:
    """
    Identifie les N premiers acheteurs d'un token, dans l'ordre
    chronologique, en excluant le dev lui-même. Réutilise le même
    décodage brut de transaction que backtest.py (pas de nouvelle
    dépendance externe).

    Retourne : [{"buyer": str, "block_time": int, "signature": str}, ...]
    triée du plus ancien au plus récent achat.
    """
    all_sigs = await wallet_history.get_all_signatures_paginated(token_mint, max_pages=5, page_size=1000)
    if not all_sigs:
        return []

    chronological = list(reversed(all_sigs))  # plus ancien -> plus récent
    semaphore = asyncio.Semaphore(5)
    buyers = []
    seen = set()

    async def _check_one(sig_info):
        async with semaphore:
            tx = await wallet_history._get_raw_transaction(sig_info["signature"])
            if not tx or not wallet_history._transaction_involves_program(tx, config.PUMP_FUN_PROGRAM_ID):
                return None
            buyer = _extract_buyer_if_buy(tx, dev_address)
            if not buyer:
                return None
            return {"buyer": buyer, "block_time": tx.get("blockTime") or sig_info.get("blockTime"),
                    "signature": sig_info["signature"]}

    # Traitement par lots de 5 (même modèle que backtest.py) — s'arrête dès
    # que N acheteurs UNIQUES sont trouvés, pas besoin de tout décoder.
    for i in range(0, len(chronological), 5):
        if len(buyers) >= n:
            break
        batch = chronological[i:i + 5]
        results = await asyncio.gather(*[_check_one(s) for s in batch])
        for r in results:
            if r and r["buyer"] not in seen and len(buyers) < n:
                seen.add(r["buyer"])
                buyers.append(r)

    return buyers


def _extract_buyer_if_buy(tx: dict, dev_address: str = None) -> str:
    """Identifie le compte dont le solde de token AUGMENTE dans cette
    transaction (= un achat), en excluant le dev."""
    try:
        meta = tx.get("meta", {})
        pre_tb = meta.get("preTokenBalances", []) or []
        post_tb = meta.get("postTokenBalances", []) or []
    except (KeyError, TypeError):
        return None

    def _amounts(balances):
        result = {}
        for b in balances:
            owner = b.get("owner")
            if not owner:
                continue
            try:
                result[owner] = float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
            except (TypeError, ValueError):
                result[owner] = 0
        return result

    pre_amounts, post_amounts = _amounts(pre_tb), _amounts(post_tb)
    for owner in set(list(pre_amounts.keys()) + list(post_amounts.keys())):
        delta = post_amounts.get(owner, 0) - pre_amounts.get(owner, 0)
        if delta > 0 and owner != dev_address:
            return owner
    return None


# ════════════════════════════════════════════════════════════════
# ÉTAPE C1 — Double signature (détection de bot externe)
# ════════════════════════════════════════════════════════════════

# Liste de programmes de bots/routeurs connus — MAINTENUE MANUELLEMENT,
# forcément incomplète. À enrichir au fil du temps si de vrais cas sont
# identifiés.
KNOWN_BOT_PROGRAM_IDS = {
    # "adresse_du_programme": "Nom du bot"
    # (liste vide au départ — aucune adresse vérifiée avec certitude au
    # moment de l'écriture ; à compléter manuellement)
}


async def check_bot_signature(token_mint: str, buyer_address: str) -> dict:
    """
    ⚠️ APPROXIMATION FAIBLE, À UTILISER AVEC PRUDENCE.

    Il n'existe PAS de registre officiel des programmes de bots sniper sur
    Solana, et la plupart des bots modernes utilisent des wallets "propres"
    indiscernables d'un humain au niveau des données on-chain publiques.
    Cette fonction vérifie seulement si la transaction d'achat implique un
    programme de la liste KNOWN_BOT_PROGRAM_IDS (vide par défaut, à remplir
    toi-même si tu identifies de vraies adresses).

    Un résultat "bot_detected": False ne PROUVE PAS que l'achat est humain
    — ça veut juste dire qu'aucun programme connu n'a été trouvé. Ne jamais
    utiliser ce critère seul pour rejeter un candidat.
    """
    signatures = await wallet_history.get_all_signatures_paginated(token_mint, max_pages=2, page_size=1000)
    tx = None
    for sig_info in reversed(signatures or []):
        candidate = await wallet_history._get_raw_transaction(sig_info["signature"])
        if candidate and _extract_buyer_if_buy(candidate) == buyer_address:
            tx = candidate
            break

    if not tx:
        return {"bot_detected": None, "matched_programs": [],
                "reason": "Transaction d'achat introuvable pour cette adresse"}

    if not KNOWN_BOT_PROGRAM_IDS:
        return {"bot_detected": None, "matched_programs": [],
                "reason": "⚠️ Liste de programmes bots vide — vérification non effectuée, "
                          "complète KNOWN_BOT_PROGRAM_IDS dans copytrade_checklist.py"}

    try:
        account_keys = tx["transaction"]["message"]["accountKeys"]
        program_ids_in_tx = {acc.get("pubkey") if isinstance(acc, dict) else acc for acc in account_keys}
    except (KeyError, TypeError):
        return {"bot_detected": None, "matched_programs": [], "reason": "Erreur de lecture de la transaction"}

    matched = [name for pid, name in KNOWN_BOT_PROGRAM_IDS.items() if pid in program_ids_in_tx]
    return {
        "bot_detected": bool(matched),
        "matched_programs": matched,
        "reason": f"Programme(s) bot connu(s) détecté(s) : {', '.join(matched)}" if matched
                  else "Aucun programme bot connu détecté (ne garantit PAS un achat humain — voir avertissement)",
    }


# ════════════════════════════════════════════════════════════════
# ÉTAPE C2 — Comparaison bundle (achat dans le même bloc que la création)
# ════════════════════════════════════════════════════════════════

async def check_bundle_buy(token_mint: str, buyer_address: str) -> dict:
    """
    Vérifie si l'achat de `buyer_address` a eu lieu dans le MÊME SLOT
    (bloc) que la création du token — signe fort d'un bundle Jito (achat
    groupé programmé au lancement), typique d'un insider ou d'un bot
    coordonné avec le dev, pas d'une découverte organique du token.

    Utilise directement le champ "slot" retourné par getSignaturesForAddress
    (déjà présent dans chaque signature, pas besoin de décoder la
    transaction entière juste pour ça).
    """
    signatures = await wallet_history.get_all_signatures_paginated(token_mint, max_pages=5, page_size=1000)
    if not signatures:
        return {"same_block": None, "reason": "Aucune signature trouvée pour ce token"}

    chronological = list(reversed(signatures))
    creation_slot = chronological[0].get("slot") if chronological else None

    buy_slot = None
    semaphore = asyncio.Semaphore(5)

    async def _find_buyer_slot(sig_info):
        async with semaphore:
            tx = await wallet_history._get_raw_transaction(sig_info["signature"])
            if tx and _extract_buyer_if_buy(tx) == buyer_address:
                return sig_info.get("slot")
            return None

    for i in range(0, len(chronological), 5):
        if buy_slot is not None:
            break
        batch = chronological[i:i + 5]
        results = await asyncio.gather(*[_find_buyer_slot(s) for s in batch])
        for r in results:
            if r is not None:
                buy_slot = r
                break

    if creation_slot is None or buy_slot is None:
        return {"same_block": None, "reason": "Slot de création ou d'achat introuvable"}

    same_block = (creation_slot == buy_slot)
    return {
        "same_block": same_block,
        "creation_slot": creation_slot,
        "buy_slot": buy_slot,
        "reason": "Achat bundlé (même bloc que la création — signe d'insider/bot coordonné)" if same_block
                  else f"Achat {buy_slot - creation_slot} slot(s) après la création (organique)",
    }


# ════════════════════════════════════════════════════════════════
# ÉTAPE D — Fréquence d'achat
# ════════════════════════════════════════════════════════════════

async def check_buy_frequency(wallet_address: str, max_results: int = 20) -> dict:
    """
    Calcule l'intervalle moyen entre deux achats consécutifs du wallet.
    Un rythme plus rapide qu'environ 1 achat/heure en moyenne est considéré
    suspect (plus cohérent avec un bot qu'avec un trader humain qui
    analyse chaque token avant d'acheter) — seuil approximatif, pas une
    règle stricte.
    """
    buys = await wallet_history.get_recent_buys(wallet_address, max_results=max_results)
    times = sorted([b["block_time"] for b in buys if b.get("block_time")])
    if len(times) < 2:
        return {"avg_interval_minutes": None, "suspiciously_fast": None,
                "reason": "Pas assez d'achats avec horodatage pour calculer une fréquence"}

    intervals = [(times[i + 1] - times[i]) / 60 for i in range(len(times) - 1)]
    avg_interval = sum(intervals) / len(intervals)
    suspiciously_fast = avg_interval < 60

    return {
        "avg_interval_minutes": avg_interval,
        "suspiciously_fast": suspiciously_fast,
        "reason": f"Intervalle moyen : {avg_interval:.0f} min entre achats"
                  + (" — plus rapide qu'1/heure, possible bot" if suspiciously_fast else " — rythme humain plausible"),
    }


# ════════════════════════════════════════════════════════════════
# ÉTAPE E — Solde jamais à zéro avant (vs jamais utilisé)
# ════════════════════════════════════════════════════════════════

async def check_continuous_balance(wallet_address: str) -> dict:
    """
    ⚠️ APPROXIMATION — la doc d'origine ne précise pas de seuil exact.

    Distingue un wallet "jamais utilisé" (fresh, voir
    wallet_history.is_wallet_fresh_before) d'un wallet "établi" dont
    l'activité suggère une utilisation continue plutôt qu'un usage
    jetable unique. Approxime "établi" par un nombre minimum de
    transactions connues (seuil arbitraire de 20, à ajuster si besoin —
    aucune définition plus précise disponible dans la documentation
    d'origine du projet).
    """
    signatures = await wallet_history.get_all_signatures_paginated(wallet_address, max_pages=2, page_size=1000)
    if not signatures:
        return {"established": False, "tx_count": 0, "reason": "Aucune transaction trouvée"}

    tx_count = len(signatures)
    is_established = tx_count >= 20

    return {
        "established": is_established,
        "tx_count": tx_count,
        "reason": f"{tx_count} transaction(s) connue(s) — "
                  + ("wallet actif et établi" if is_established else "activité limitée, possiblement un wallet jetable"),
    }


# ════════════════════════════════════════════════════════════════
# ÉTAPE F — Win rate sur 7 jours
# ════════════════════════════════════════════════════════════════

async def get_win_rate_7d(wallet_address: str, tp_pct: float = None, sl_pct: float = None) -> dict:
    """
    Win rate calculé UNIQUEMENT sur les achats des 7 derniers jours —
    contrairement à analyze_copytrade_candidate.py qui backteste les N
    derniers achats sans limite de temps. Réutilise
    backtest.get_detailed_trade_info() (même méthode "MC max après
    entrée" que partout ailleurs dans le bot).
    """
    seven_days_ago = int(time_module.time()) - (7 * 24 * 3600)
    buys = await wallet_history.get_recent_buys(wallet_address, max_results=30)
    recent_buys = [b for b in buys if (b.get("block_time") or 0) >= seven_days_ago]

    if not recent_buys:
        return {"win_rate": None, "trade_count": 0, "reason": "Aucun achat dans les 7 derniers jours"}

    results = []
    for t in recent_buys:
        detail = await backtest.get_detailed_trade_info(
            t["token_mint"], purchase_block_time=t.get("block_time"), tp_pct=tp_pct, sl_pct=sl_pct,
            sol_spent=t.get("sol_spent"), tokens_received=t.get("tokens_received"),
        )
        results.append(detail)

    wins = [r for r in results if r["result_pct"] > 0]
    win_rate = len(wins) / len(results) * 100
    return {"win_rate": win_rate, "trade_count": len(results),
            "reason": f"{len(wins)}/{len(results)} trade(s) gagnant(s) sur les 7 derniers jours"}


# ════════════════════════════════════════════════════════════════
# ÉTAPE G — Cohérence des points d'entrée (market cap)
# ════════════════════════════════════════════════════════════════

async def check_entry_consistency(wallet_address: str, max_results: int = 10) -> dict:
    """
    Vérifie si le trader achète systématiquement dans une fourchette de
    market cap similaire (signe d'une vraie stratégie répétable) via le
    coefficient de variation (écart-type / moyenne) des MC d'entrée —
    plus bas = plus cohérent. Seuil de 0.5 arbitraire (pas de définition
    précise dans la doc d'origine).
    """
    buys = await wallet_history.get_recent_buys(wallet_address, max_results=max_results)
    if len(buys) < 3:
        return {"consistent": None, "reason": "Pas assez d'achats pour évaluer la cohérence (minimum 3)"}

    entry_mcs = []
    for t in buys:
        detail = await backtest.get_detailed_trade_info(
            t["token_mint"], purchase_block_time=t.get("block_time"),
            sol_spent=t.get("sol_spent"), tokens_received=t.get("tokens_received"),
        )
        if detail.get("entry_market_cap_usd"):
            entry_mcs.append(detail["entry_market_cap_usd"])

    if len(entry_mcs) < 3:
        return {"consistent": None, "reason": "Market cap d'entrée indisponible pour assez de trades"}

    mean_mc = statistics.mean(entry_mcs)
    stdev_mc = statistics.stdev(entry_mcs)
    coefficient_variation = (stdev_mc / mean_mc) if mean_mc > 0 else None
    consistent = coefficient_variation is not None and coefficient_variation < 0.5

    return {
        "consistent": consistent,
        "mean_entry_mc": mean_mc,
        "coefficient_variation": coefficient_variation,
        "reason": (f"MC d'entrée moyen : ${mean_mc:,.0f}, variation : {coefficient_variation:.2f} "
                   f"({'cohérent' if consistent else 'dispersé'})") if coefficient_variation is not None
                  else "Impossible de calculer la variation",
    }


# ════════════════════════════════════════════════════════════════
# AGRÉGATEUR — lance B à G en un seul appel (H reste séparé, voir
# analyze_copytrade_candidate.py — déjà construit, pas dupliqué ici)
# ════════════════════════════════════════════════════════════════

async def run_full_checklist(token_mint: str, buyer_address: str, dev_address: str = None) -> dict:
    """
    Lance les étapes B, C1, C2, D, E, F, G pour un wallet donné, sur un
    token de référence. Ne relance PAS l'étape H (rentabilité) — utilise
    analyze_copytrade_candidate.py séparément pour ça, déjà existant.

    Retourne un dict avec une clé par étape.
    """
    first_buyers = await get_first_n_buyers(token_mint, n=10, dev_address=dev_address)
    is_early_buyer = buyer_address in [b["buyer"] for b in first_buyers]

    bot_check = await check_bot_signature(token_mint, buyer_address)
    bundle_check = await check_bundle_buy(token_mint, buyer_address)
    frequency_check = await check_buy_frequency(buyer_address)
    balance_check = await check_continuous_balance(buyer_address)
    win_rate_7d = await get_win_rate_7d(buyer_address)
    entry_consistency = await check_entry_consistency(buyer_address)

    return {
        "B_early_buyer": {"is_early_buyer": is_early_buyer, "first_buyers_count": len(first_buyers)},
        "C1_bot_signature": bot_check,
        "C2_bundle": bundle_check,
        "D_frequency": frequency_check,
        "E_continuous_balance": balance_check,
        "F_win_rate_7d": win_rate_7d,
        "G_entry_consistency": entry_consistency,
    }
