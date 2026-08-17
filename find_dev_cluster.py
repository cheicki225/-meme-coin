"""
════════════════════════════════════════════════════════════════
FIND DEV CLUSTER — Suit un dev à travers ses changements d'adresse
════════════════════════════════════════════════════════════════
Les vidéos le disent explicitement : "les reugers en général ils changent
d'adresse quand ils font des tokens... il va en créer une deuxième, une
troisième..." — find_dev.py et find_all_dev_tokens.py ne cherchent qu'UNE
adresse, donc ratent tout l'historique créé sous d'autres adresses par le
même dev.

Cet outil résout ça en 3 étapes :
1. Trouve qui a financé l'adresse de départ (fund_tracer.py)
2. Trouve TOUTES les autres adresses financées par cette même source
   (pattern_detector.py — même montant fixe si schéma "exchange", ou tous
   les destinataires si schéma "mère")
3. Cherche les tokens créés par CHAQUE adresse du cluster, pas juste la
   première (wallet_history.py)

LIMITE HONNÊTE : ne fonctionne que si le dev réutilise la MÊME source de
financement à chaque nouvelle adresse (comme décrit dans les vidéos — montant
fixe depuis un exchange, ou une adresse mère commune). Si le dev change aussi
de source de financement à chaque fois, ce cluster ne le suivra pas — rien
ne permet de le détecter dans ce cas sans un signal supplémentaire (ex: style
de token, timing, etc.) qu'on n'a pas construit.

Usage :
    python find_dev_cluster.py <adresse_dev_ou_token>
"""

import asyncio
import sys
import time

import config
import find_dev
import fund_tracer
import pattern_detector
import backtest
import wallet_history


def _progress(page, info):
    if page is not None:
        print(f"   📄 Page {page} — {info} signatures au total...")
    else:
        print(f"   ⏳ {info}")


async def find_cluster_addresses(dev_address: str, max_starred: int = 12, on_progress=None) -> dict:
    """
    Trouve les autres adresses probablement liées au même dev, via leur
    source de financement commune. Retourne :
    {"funder": str, "scheme": str, "cluster": [adresses...], "neutral": [adresses...]}

    cluster = adresses avec au moins 1 token créé ("étoilées", comme le
    transcript) — celles-ci auront droit au scan complet coûteux.
    neutral = adresses matchant le montant mais SANS création connue —
    listées pour info, mais pas scannées en profondeur (économise du temps).

    max_starred : nombre maximum d'adresses ⭐ à trouver avant d'arrêter la
    recherche — LIMITÉ à 12 par défaut suite à une demande explicite (vérifier
    92 adresses une par une était trop lent et consommait trop de quota pour
    peu de gain). Passe None pour vérifier TOUTES les adresses trouvées, sans
    limite (plus lent, mais exhaustif si tu en as vraiment besoin).

    on_progress : callback optionnel appelé avec un simple texte (str) pour
    chaque étape importante — AJOUTÉ pour permettre la réutilisation de
    cette fonction depuis le bot Telegram ("Analyse du token"), où print()
    est invisible. Par défaut (None), continue d'utiliser print() pour
    préserver le comportement du script terminal.
    """
    def _report(text: str):
        if on_progress:
            on_progress(text)
        else:
            print(text)

    funder_info = await fund_tracer.get_first_funder(dev_address)
    if not funder_info or not funder_info.get("funder_address"):
        return {"funder": None, "scheme": None, "cluster": [dev_address], "neutral": []}

    funder = funder_info["funder_address"]
    scheme = funder_info["scheme"]
    amount = funder_info["amount_sol"]

    if scheme == "exchange":
        # Le dev est financé par un montant fixe depuis un exchange —
        # cherche d'autres adresses ayant reçu le MÊME montant.
        # Calibré sur des vraies données (voir README) : les vrais retraits
        # d'exchange varient jusqu'à ~0.5% autour du montant cible (frais
        # réseau, arrondis internes) — 0.001% était bien trop strict et
        # ratait systématiquement les vrais transferts. 0.5% couvre l'écart
        # maximum observé (0.49%) avec une petite marge.
        tolerance_pct = 0.00001  # 0.001% (restauré — régression accidentelle lors d'une réécriture précédente)
        tolerance_sol = amount * tolerance_pct
        lo = amount - tolerance_sol
        hi = amount + tolerance_sol
        _report(f"   Montant de référence (1er dépôt) : {amount} SOL")
        _report(f"   Tolérance ({tolerance_pct*100:.3f}%)                : ±{tolerance_sol:.8f} SOL")
        _report(f"   Intervalle recherché              : [{lo:.8f} ; {hi:.8f}] SOL\n")
        _report(f"   ⏳ Recherche en cours (peut prendre plusieurs minutes sur une adresse à fort volume comme un exchange)...")
        progress_cb = (lambda page, info: on_progress(f"Page {page} — {info} signatures au total..." if page is not None else info)) if on_progress else _progress
        recipients = await pattern_detector.find_fixed_amount_recipients(
            funder, amount, tolerance=tolerance_sol, on_progress=progress_cb,
        )
    else:
        # Schéma "mère" ou "simple" — cherche tous les destinataires de cette
        # source (moins précis si "simple", mais on tente quand même).
        recipients = await pattern_detector.find_all_outgoing_recipients(funder, max_results=50)

    unique_recipients = {r["recipient"] for r in recipients} - {dev_address}

    # ── Tri étoile/neutre (comme les "petites étoiles" du transcript) ──
    # Vérification LÉGÈRE (has_created_any_token, s'arrête tôt) sur chaque
    # adresse trouvée AVANT d'investir dans un scan complet coûteux.
    # LIMITÉ à max_starred (12 par défaut) : s'arrête dès que ce nombre
    # d'adresses ⭐ est trouvé, au lieu de vérifier TOUTES les adresses
    # trouvées (92 dans un cas réel — beaucoup trop lent et consommateur
    # de quota). Passe max_starred=None pour vérifier sans limite.
    _report(f"\n   🔍 Vérification rapide de {len(unique_recipients)} adresse(s) trouvée(s) (a-t-elle créé un token ?)...")
    wallet_history.reset_stats()
    unique_recipients_list = list(unique_recipients)
    starred_info = []
    neutral = []

    for i, addr in enumerate(unique_recipients_list):
        creation = await wallet_history.has_created_any_token(addr)
        if creation:
            starred_info.append({"address": addr, "mint": creation["mint"], "block_time": creation.get("block_time")})
            if on_progress and (i + 1) % 5 == 0:
                on_progress(f"{i + 1}/{len(unique_recipients_list)} adresses vérifiées, {len(starred_info)} ⭐ trouvée(s)...")
        else:
            neutral.append(addr)

        if max_starred is not None and len(starred_info) >= max_starred:
            remaining = len(unique_recipients_list) - (i + 1)
            _report(f"\n   ✅ {max_starred} adresse(s) ⭐ trouvée(s) — arrêt anticipé ({remaining} adresse(s) restante(s) non vérifiée(s)).")
            break

    stats = wallet_history.get_stats()
    total_checked = stats["mobula_hits"] + stats["helius_fallback"]
    if total_checked > 0:
        _report(f"\n   📊 Répartition : {stats['mobula_hits']} résolu(s) via Mobula (0 coût Helius), "
                f"{stats['helius_fallback']} en repli sur Helius (Mobula ne les connaissait pas).")

    # Tri du plus récent au plus ancien — block_time=None (jamais déterminé)
    # relégué à la fin plutôt que de planter ou fausser l'ordre.
    starred_info.sort(key=lambda x: x["block_time"] if x["block_time"] is not None else -1, reverse=True)

    if not on_progress:
        print(f"\n   ✅ {len(starred_info)} token(s) trouvé(s) :")
        for s in starred_info:
            print(f"      {s['mint']}")

    cluster = [dev_address] + [s["address"] for s in starred_info]
    return {"funder": funder, "scheme": scheme, "cluster": cluster, "neutral": neutral, "starred_info": starred_info}


async def main(input_address: str, max_starred: int = 12):
    print(f"🔍 Recherche du cluster de wallets pour : {input_address}\n")

    if not config.HELIUS_API_KEY:
        print("❌ Aucune clé HELIUS_API_KEY configurée dans .env.")
        return

    # Si c'est une adresse de token plutôt qu'un dev, retrouve d'abord le dev
    dev_address = input_address
    if len(input_address) > 40 and input_address.endswith("pump"):
        print("── Adresse de token détectée, recherche du créateur d'abord ──")
        creator_info = await find_dev.find_token_creator(input_address)
        if not creator_info:
            print("❌ Impossible de trouver le créateur de ce token.")
            return
        dev_address = creator_info["dev_address"]
        print(f"✅ Dev trouvé : {dev_address}\n")

    print("── Étape 1/3 : recherche du cluster d'adresses liées ──")
    cluster_info = await find_cluster_addresses(dev_address, max_starred=max_starred)
    cluster = cluster_info["cluster"]
    neutral = cluster_info.get("neutral", [])

    print(f"\nFinanceur commun : {cluster_info['funder'] or 'introuvable'}")
    print(f"Schéma : {cluster_info['scheme'] or 'inconnu'}")
    print(f"\n⭐ Adresses retenues pour le scan complet ({len(cluster)}) :")
    for addr in cluster:
        marker = " (adresse de départ)" if addr == dev_address else ""
        print(f"   - {addr}{marker}")
    if neutral:
        print(f"\n⚪ Adresses ignorées, montant matché mais aucun token créé ({len(neutral)}) :")
        for addr in neutral:
            print(f"   - {addr}")
    print()

    print("── Étape 2/3 : recherche des tokens créés par CHAQUE adresse retenue (⭐) ──")
    all_tokens = []
    for i, addr in enumerate(cluster):
        print(f"\n   Adresse {i+1}/{len(cluster)} : {addr[:12]}...")
        tokens = await wallet_history.get_all_created_tokens(addr, max_pages=10, on_progress=_progress)
        for t in tokens:
            t["creator_address"] = addr
        all_tokens.extend(tokens)
        print(f"   → {len(tokens)} token(s) trouvé(s) pour cette adresse")

    print(f"\n{'='*60}")
    print(f"✅ TOTAL : {len(all_tokens)} token(s) créé(s) trouvé(s) à travers {len(cluster)} adresse(s)")
    print(f"{'='*60}\n")

    # Trié du plus récent au plus ancien, limité aux N les plus récents —
    # à travers TOUT le cluster (le dev + les adresses liées via l'exchange
    # intermédiaire), pas juste une seule adresse.
    recent_count = 10
    all_tokens_sorted = sorted(all_tokens, key=lambda t: t.get("block_time") or -1, reverse=True)
    recent_tokens = all_tokens_sorted[:recent_count]

    print(f"📋 Les {len(recent_tokens)} plus récents (sur {len(all_tokens)} au total, tout le cluster confondu) :")
    for i, t in enumerate(recent_tokens):
        date_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(t["block_time"])) if t.get("block_time") else "date inconnue"
        print(f"  [{i+1}] {t['token_mint']}  (créé par {t['creator_address'][:8]}..., {date_str})")

    print(f"\n── Étape 3/3 : backtest sur les {len(recent_tokens)} tokens les plus récents du cluster ──")
    if len(recent_tokens) < 3:
        print("⏭️  Historique insuffisant (< 3 tokens) pour un backtest fiable.")
        return

    tp_pct = 100.0
    print(f"   Seuil TP testé : +{tp_pct:.0f}% | SL du bot : -{config.SL_PCT:.0f}%\n")

    results = []
    for t in recent_tokens:
        result = await backtest.backtest_token(t["token_mint"], tp_pct=tp_pct, sl_pct=config.SL_PCT)
        results.append(result)
        status_icon = "✅" if result["hit_tp"] else ("❌" if result["hit_sl"] else "➖")
        print(f"  {status_icon} {t['token_mint'][:12]}... → {result['result_pct']:+7.1f}%  ({result['reason']})")

    total_pct = sum(r["result_pct"] for r in results)
    wins = [r for r in results if r["result_pct"] > 0]
    losses = [r for r in results if r["result_pct"] <= 0]
    win_rate = (len(wins) / len(results) * 100) if results else 0
    total_gain = sum(r["result_pct"] for r in wins)
    total_loss = abs(sum(r["result_pct"] for r in losses))
    ratio = (total_gain / total_loss) if total_loss > 0 else float("inf") if total_gain > 0 else 0

    stake = 100
    final_value = sum(stake * (1 + r["result_pct"] / 100) for r in results)
    net_result = final_value - stake * len(results)

    print(f"\n{'='*60}")
    print(f"📈 RÉSULTATS ({len(results)} tokens les plus récents du cluster, TP +{tp_pct:.0f}%)")
    print(f"{'='*60}")
    print(f"Résultat cumulé   : {total_pct:+.1f}%")
    print(f"Win rate           : {win_rate:.0f}% ({len(wins)}/{len(results)})")
    print(f"Ratio gain/perte   : {ratio:.2f}")
    print(f"Simulation ({stake}$/token) : investi {stake*len(results):.0f}$ → final {final_value:.0f}$ ({net_result:+.0f}$)")

    verdict = "✅ Correspond aux critères" if (ratio >= config.BACKTEST_MIN_RATIO and net_result > 0) else "❌ Ne correspond pas aux critères"
    print(f"\n{verdict}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage : python find_dev_cluster.py <adresse_dev_ou_token> [--all]")
        print("   --all : vérifie TOUTES les adresses trouvées (pas de limite à 12), plus lent")
        sys.exit(1)

    address_arg = sys.argv[1]
    # --all désactive la limite (max_starred=None) — vérifie tout, comme
    # demandé explicitement ("tu peux me donner tout les adresse qui on créé
    # des token") plutôt que de s'arrêter à 12.
    max_starred_arg = None if "--all" in sys.argv else 12

    asyncio.run(main(address_arg, max_starred=max_starred_arg))
