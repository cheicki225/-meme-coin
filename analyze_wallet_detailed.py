"""
════════════════════════════════════════════════════════════════
ANALYZE WALLET DETAILED — Détail complet de chaque achat d'un wallet
════════════════════════════════════════════════════════════════
Contrairement à analyze_copytrade_candidate.py (qui donne juste le résultat
du backtest par token), celui-ci affiche TOUT ce qu'on peut savoir sur
chaque token acheté : nom, prix, market cap, liquidité, volume 24h, date
d'achat, lien DexScreener — sans limite de longueur (script terminal, pas
un message Telegram).

Usage :
    python analyze_wallet_detailed.py --wallet <adresse> [--count 10]
"""

import argparse
import asyncio
import sys

import config
import wallet_history
import backtest


def _fmt(value, prefix="", suffix="", decimals=2):
    """Formate proprement une valeur potentiellement None."""
    if value is None:
        return "N/A"
    try:
        return f"{prefix}{float(value):,.{decimals}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


async def analyze_detailed(wallet_address: str, count: int = 10):
    print(f"🔎 Détail complet des achats — wallet : {wallet_address}")
    print(f"   Les {count} derniers achats Pump.fun\n")

    buys = await wallet_history.get_recent_buys(wallet_address, max_results=count)
    if not buys:
        print("❌ Aucun achat Pump.fun trouvé pour ce wallet.")
        return

    print(f"🔍 {len(buys)} achat(s) trouvé(s) — récupération des détails complets...\n")
    print("=" * 70)

    details_list = []
    for i, t in enumerate(buys):
        detail = await backtest.get_detailed_trade_info(
            t["token_mint"], purchase_block_time=t.get("block_time"),
            tp_pct=100.0, sl_pct=config.SL_PCT,
            sol_spent=t.get("sol_spent"), tokens_received=t.get("tokens_received"),
        )
        details_list.append(detail)

        status_icon = "✅ TP ATTEINT" if detail["hit_tp"] else ("❌ SL TOUCHÉ" if detail["hit_sl"] else "➖ OUVERT")
        name_display = f"{detail['name']} ({detail['symbol']})" if detail.get("name") else "Nom inconnu"

        print(f"\n[{i+1}/{len(buys)}] {name_display}")
        print(f"   Adresse du token    : {t['token_mint']}")
        print(f"   Date d'achat        : {detail['purchase_date']}")
        print(f"   Signature           : {t.get('signature', 'N/A')}")
        print(f"   SOL dépensé (achat) : {_fmt(t.get('sol_spent'), suffix=' SOL', decimals=4)}")
        print(f"   Tokens reçus        : {_fmt(t.get('tokens_received'), decimals=0)}")
        print(f"   MC au moment d'achat: {_fmt(detail.get('entry_market_cap_usd'), prefix='$')}  (calculé depuis la vraie transaction, pas une approximation)")
        print(f"   Prix actuel         : {_fmt(detail['price_usd'], prefix='$', decimals=10)}")
        print(f"   Market Cap actuel   : {_fmt(detail['market_cap'], prefix='$')}")
        print(f"   Liquidité           : {_fmt(detail['liquidity_usd'], prefix='$')}")
        print(f"   Volume 24h          : {_fmt(detail['volume_24h'], prefix='$')}")
        print(f"   Variation 24h       : {_fmt(detail.get('price_change_24h'), suffix='%', decimals=1)}")
        print(f"   Résultat backtest   : {detail['result_pct']:+.1f}%  [{status_icon}]")
        print(f"   Raison              : {detail['reason']}")
        if detail.get("dexscreener_url"):
            print(f"   DexScreener         : {detail['dexscreener_url']}")
        print("-" * 70)

    # Résumé agrégé, même calcul que analyze_copytrade_candidate.py
    total_pct = sum(d["result_pct"] for d in details_list)
    wins = [d for d in details_list if d["result_pct"] > 0]
    losses = [d for d in details_list if d["result_pct"] <= 0]
    win_rate = (len(wins) / len(details_list) * 100) if details_list else 0
    total_gain = sum(d["result_pct"] for d in wins)
    total_loss = abs(sum(d["result_pct"] for d in losses))
    ratio = (total_gain / total_loss) if total_loss > 0 else float("inf") if total_gain > 0 else 0

    print(f"\n{'='*70}")
    print(f"📈 RÉSUMÉ AGRÉGÉ ({len(details_list)} achats)")
    print(f"{'='*70}")
    print(f"Résultat cumulé   : {total_pct:+.1f}%")
    print(f"Win rate           : {win_rate:.0f}% ({len(wins)}/{len(details_list)})")
    print(f"Ratio gain/perte   : {ratio:.2f}  (seuil recommandé : ≥{config.BACKTEST_MIN_RATIO})")

    tokens_with_mc = [d["market_cap"] for d in details_list if d.get("market_cap")]
    if tokens_with_mc:
        avg_mc = sum(tokens_with_mc) / len(tokens_with_mc)
        print(f"Market cap moyen   : ${avg_mc:,.0f}  (sur {len(tokens_with_mc)} tokens avec donnée disponible)")


async def main():
    parser = argparse.ArgumentParser(description="Détail complet de chaque achat d'un wallet.")
    parser.add_argument("--wallet", required=True, help="Adresse du wallet à analyser")
    parser.add_argument("--count", type=int, default=10, help="Nombre d'achats à analyser (défaut: 10)")
    args = parser.parse_args()

    if not config.HELIUS_API_KEY:
        print("❌ Aucune clé HELIUS_API_KEY configurée dans .env.")
        sys.exit(1)

    await analyze_detailed(args.wallet, count=args.count)


if __name__ == "__main__":
    asyncio.run(main())
