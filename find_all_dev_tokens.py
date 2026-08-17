"""
════════════════════════════════════════════════════════════════
FIND ALL DEV TOKENS — Historique complet, pas un échantillon
════════════════════════════════════════════════════════════════
Contrairement à find_dev.py (qui scanne les 300 dernières signatures,
plafonné à 15 résultats — rapide mais partiel), cet outil pagine sur TOUT
l'historique disponible du dev pour trouver la liste complète de ses
créations. Plus lent (potentiellement des centaines/milliers d'appels
réseau pour un dev très actif), à utiliser pour une analyse approfondie
ponctuelle, pas dans la boucle temps réel du bot.

Usage :
    python find_all_dev_tokens.py <adresse_dev>
"""

import asyncio
import sys
import time

import config
import wallet_history


def _progress_callback(page, info):
    if page is not None:
        print(f"   📄 Page {page} récupérée — {info} signatures au total jusqu'ici...")
    else:
        print(f"   ⏳ {info}")


async def main(dev_address: str):
    print(f"🔍 Recherche COMPLÈTE des tokens créés par : {dev_address}")
    print(f"   (pagine sur tout l'historique — peut prendre plusieurs minutes pour un dev très actif)\n")

    if not config.HELIUS_API_KEY:
        print("❌ Aucune clé HELIUS_API_KEY configurée dans .env.")
        return

    start = time.time()
    tokens = await wallet_history.get_all_created_tokens(dev_address, max_pages=20, on_progress=_progress_callback)
    elapsed = time.time() - start

    print(f"\n{'='*60}")
    print(f"✅ TERMINÉ en {elapsed:.0f}s — {len(tokens)} token(s) créé(s) trouvé(s) au total")
    print(f"{'='*60}\n")

    for i, t in enumerate(tokens):
        print(f"  [{i+1}] {t['token_mint']}")

    if not tokens:
        print("Aucun token trouvé — soit ce dev n'a jamais créé de token sur Pump.fun,")
        print("soit son historique dépasse le plafond de sécurité (20 000 signatures).")

    print(f"\nPour un backtest complet sur cette liste, utilise :")
    print(f"  python analyze_yield.py --dev {dev_address}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage : python find_all_dev_tokens.py <adresse_dev>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
