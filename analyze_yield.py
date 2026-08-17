"""
════════════════════════════════════════════════════════════════
ANALYZE YIELD — Rentabilité si on avait acheté TOUS les tokens d'un dev
════════════════════════════════════════════════════════════════
Backteste individuellement chaque token créé par un dev (achat fin 1ère
bougie, TP au seuil donné, SL par défaut de config.py), puis calcule le
rendement cumulé pour répondre à la question : "si j'avais acheté chaque
token créé par ce dev avec ce TP, aurais-je été rentable ?"

Usage :
    python analyze_yield.py --dev <adresse_du_dev> [--tp 100]
    python analyze_yield.py --token <adresse_d_un_token_du_dev> [--tp 100]

Avec --token, retrouve d'abord le dev via find_dev.py, puis analyse tout
son historique. --tp fixe le seuil de Take Profit à tester (100% par défaut,
comme demandé — "si chaque trade visait +100%, est-ce que ça reste rentable ?").

Ne fait AUCUN achat, AUCUNE modification du monitoring — outil de lecture
seule, comme find_dev.py.
"""

import argparse
import asyncio
import sys

import config
import wallet_history
import backtest
import find_dev


async def analyze_yield(dev_address: str, tp_pct: float = 100.0, count: int = 10):
    print(f"📊 Analyse de rendement — dev : {dev_address}")
    print(f"   Les {count} derniers tokens créés | Seuil TP testé : +{tp_pct:.0f}% | SL du bot : -{config.SL_PCT:.0f}%\n")

    tokens = await wallet_history.get_created_tokens(dev_address, max_results=count)
    if not tokens:
        print("❌ Aucun token trouvé pour ce dev (historique vide ou erreur réseau).")
        return

    print(f"🔍 {len(tokens)} token(s) trouvé(s) (sur les {count} demandés) — backtest en cours...\n")

    results = []
    for i, t in enumerate(tokens):
        # sl_pct explicitement passé = le VRAI SL configuré dans ton bot,
        # pas une valeur par défaut potentiellement différente de la fonction.
        result = await backtest.backtest_token(t["token_mint"], tp_pct=tp_pct, sl_pct=config.SL_PCT)
        results.append(result)
        status_icon = "✅" if result["hit_tp"] else ("❌" if result["hit_sl"] else "➖")
        print(f"  {status_icon} {t['token_mint'][:12]}... → {result['result_pct']:+7.1f}%  ({result['reason']})")

    # ── Agrégation ────────────────────────────────────────────────
    total_pct = sum(r["result_pct"] for r in results)
    wins = [r for r in results if r["result_pct"] > 0]
    losses = [r for r in results if r["result_pct"] <= 0]
    win_rate = (len(wins) / len(results) * 100) if results else 0
    avg_per_trade = total_pct / len(results) if results else 0

    total_gain = sum(r["result_pct"] for r in wins)
    total_loss = abs(sum(r["result_pct"] for r in losses))
    ratio = (total_gain / total_loss) if total_loss > 0 else float("inf") if total_gain > 0 else 0

    print(f"\n{'='*60}")
    print(f"📈 RÉSULTATS AGRÉGÉS ({len(results)} tokens, TP fixé à +{tp_pct:.0f}%)")
    print(f"{'='*60}")
    print(f"Résultat cumulé (somme simple)  : {total_pct:+.1f}%")
    print(f"Résultat moyen par trade         : {avg_per_trade:+.1f}%")
    print(f"Win rate                         : {win_rate:.0f}% ({len(wins)}/{len(results)})")
    print(f"Ratio gain/perte                 : {ratio:.2f}")
    print()

    # ── Simulation avec montant fixe par trade (plus parlant que le %) ─
    stake_per_trade = 100  # unité arbitraire (ex: 100$ par trade) pour illustrer en valeur absolue
    final_value = 0
    for r in results:
        final_value += stake_per_trade * (1 + r["result_pct"] / 100)
    total_invested = stake_per_trade * len(results)
    net_result = final_value - total_invested
    net_pct = (net_result / total_invested * 100) if total_invested > 0 else 0

    print(f"💰 SIMULATION (en misant {stake_per_trade}$ sur CHAQUE token, {len(results)} trades) :")
    print(f"   Total investi : {total_invested:.0f}$")
    print(f"   Valeur finale : {final_value:.0f}$")
    print(f"   Résultat net  : {net_result:+.0f}$ ({net_pct:+.1f}%)")
    print()

    if net_result > 0:
        print(f"✅ VERDICT : rentable — en achetant systématiquement tous les tokens de ce dev")
        print(f"   avec un TP à +{tp_pct:.0f}%, le résultat net aurait été positif.")
    else:
        print(f"❌ VERDICT : PAS rentable — même avec un TP à +{tp_pct:.0f}%, le résultat net")
        print(f"   aurait été négatif sur cet historique. Ne pas suivre ce dev tel quel.")

    print(f"{'='*60}")


async def main():
    parser = argparse.ArgumentParser(description="Analyse le rendement des N derniers tokens créés par un dev.")
    parser.add_argument("--dev", help="Adresse du wallet dev à analyser directement")
    parser.add_argument("--token", help="Adresse d'un token créé par le dev (le dev sera retrouvé automatiquement)")
    parser.add_argument("--tp", type=float, default=100.0, help="Seuil de Take Profit à tester en %% (défaut: 100)")
    parser.add_argument("--count", type=int, default=10, help="Nombre de tokens récents à analyser (défaut: 10)")
    args = parser.parse_args()

    if not args.dev and not args.token:
        print("❌ Fournis soit --dev <adresse_dev>, soit --token <adresse_token>.")
        sys.exit(1)

    if not config.HELIUS_API_KEY:
        print("❌ Aucune clé HELIUS_API_KEY configurée dans .env — impossible de continuer.")
        sys.exit(1)

    dev_address = args.dev
    if not dev_address:
        print(f"🔍 Recherche du dev à partir du token {args.token}...\n")
        creator_info = await find_dev.find_token_creator(args.token)
        if not creator_info:
            print("❌ Impossible de retrouver le dev à partir de ce token.")
            sys.exit(1)
        dev_address = creator_info["dev_address"]
        print(f"✅ Dev trouvé : {dev_address}\n")

    await analyze_yield(dev_address, tp_pct=args.tp, count=args.count)


if __name__ == "__main__":
    asyncio.run(main())
