"""
════════════════════════════════════════════════════════════════
ANALYZE COPYTRADE CANDIDATE — Valider un wallet avant de le copier
════════════════════════════════════════════════════════════════
Contrairement à analyze_yield.py (qui teste les tokens CRÉÉS par un dev),
cet outil teste les tokens ACHETÉS par un trader — exactement l'étape H
du transcript (méthode 2, copy trading) : "on va regarder les graphiques
des 10 derniers tokens achetés et calculer si on aurait été profitable
avec TP à 100% / perte max à -30%".

Usage :
    python analyze_copytrade_candidate.py --wallet <adresse> [--tp 100] [--count 10]

Ne fait AUCUN achat, AUCUNE modification du monitoring — lecture seule.
"""

import argparse
import asyncio
import sys

import config
import wallet_history
import backtest


async def analyze_candidate(wallet_address: str, tp_pct: float = 100.0, count: int = 10):
    print(f"📊 Analyse du candidat copy trading : {wallet_address}")
    print(f"   Les {count} derniers ACHATS | Seuil TP testé : +{tp_pct:.0f}% | SL du bot : -{config.SL_PCT:.0f}%\n")

    buys = await wallet_history.get_recent_buys(wallet_address, max_results=count)
    if not buys:
        print("❌ Aucun achat Pump.fun trouvé pour ce wallet (historique vide, erreur réseau,")
        print("   ou ce wallet n'achète pas sur Pump.fun).")
        return

    print(f"🔍 {len(buys)} achat(s) trouvé(s) (sur les {count} demandés) — backtest en cours...\n")

    results = []
    for idx, t in enumerate(buys):
        print(f"  [{idx + 1}/{len(buys)}] {t['token_mint'][:12]}... — analyse en cours (scan on-chain, peut prendre du temps)", flush=True)
        # CORRIGÉ : utilise get_detailed_trade_info() (prix d'entrée RÉEL,
        # calculé depuis sol_spent/tokens_received de la transaction d'achat
        # elle-même) au lieu de backtest_token() qui se basait sur
        # price_change_h24 de DexScreener — une variation générique sur les
        # dernières 24h sans aucun lien avec le moment réel où CE wallet a
        # acheté. Ça faisait taper le SL quasi systématiquement pour tout
        # token déjà dumpé au moment du test, même si le trade avait en
        # réalité été profitable à l'entrée réelle du wallet.
        result = await backtest.get_detailed_trade_info(
            t["token_mint"],
            purchase_block_time=t.get("block_time"),
            tp_pct=tp_pct,
            sl_pct=config.SL_PCT,
            sol_spent=t.get("sol_spent"),
            tokens_received=t.get("tokens_received"),
        )
        results.append(result)
        status_icon = "✅" if result["hit_tp"] else ("❌" if result["hit_sl"] else "➖")
        entry_mc = f" (MC entrée: ${result['entry_market_cap_usd']:,.0f})" if result.get("entry_market_cap_usd") else ""
        print(f"  {status_icon} {t['token_mint'][:12]}... → {result['result_pct']:+7.1f}%{entry_mc}  ({result['reason']})")

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
    print(f"📈 RÉSULTATS ({len(results)} achats, TP +{tp_pct:.0f}% / SL -{config.SL_PCT:.0f}%)")
    print(f"{'='*60}")
    print(f"Résultat cumulé   : {total_pct:+.1f}%")
    print(f"Win rate           : {win_rate:.0f}% ({len(wins)}/{len(results)})")
    print(f"Ratio gain/perte   : {ratio:.2f}  (seuil recommandé : ≥{config.BACKTEST_MIN_RATIO})")
    print(f"Simulation ({stake}$/trade) : investi {stake*len(results):.0f}$ → final {final_value:.0f}$ ({net_result:+.0f}$)")

    if ratio >= config.BACKTEST_MIN_RATIO and net_result > 0:
        print(f"\n✅ VERDICT : candidat intéressant pour le copy trading.")
        print(f"   Pour l'ajouter : mode Track Buy (pas Track Creation), TP à +{tp_pct:.0f}%.")
    else:
        print(f"\n❌ VERDICT : ne correspond pas aux critères — pas recommandé de copier ce wallet tel quel.")

    print(f"\n⚠️  Rappel (étape H du transcript) : ce test suppose un achat à la fin de la")
    print(f"   1ère bougie après l'achat du wallet suivi, comme le bot le ferait en LIVE.")
    print(f"   Vérifie aussi la fréquence d'achat (max ~1/heure recommandé) et la régularité")
    print(f"   des points d'entrée — ce script ne teste que la rentabilité, pas ces 2 points.")


async def main():
    parser = argparse.ArgumentParser(description="Valide un wallet candidat au copy trading (teste ses derniers achats).")
    parser.add_argument("--wallet", required=True, help="Adresse du wallet trader à évaluer")
    parser.add_argument("--tp", type=float, default=100.0, help="Seuil de Take Profit à tester en %% (défaut: 100)")
    parser.add_argument("--count", type=int, default=10, help="Nombre d'achats récents à analyser (défaut: 10)")
    args = parser.parse_args()

    if not config.HELIUS_API_KEY:
        print("❌ Aucune clé HELIUS_API_KEY configurée dans .env.")
        sys.exit(1)

    await analyze_candidate(args.wallet, tp_pct=args.tp, count=args.count)


if __name__ == "__main__":
    asyncio.run(main())
