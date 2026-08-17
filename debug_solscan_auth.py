"""
════════════════════════════════════════════════════════════════
DEBUG SOLSCAN AUTH — Teste plusieurs formats de header en une fois
════════════════════════════════════════════════════════════════
Usage : python debug_solscan_auth.py
(utilise automatiquement SOLSCAN_API_KEY depuis ton .env)
"""

import asyncio
import aiohttp
from aiohttp.resolver import ThreadedResolver
import config


async def try_variant(session, label, headers, params):
    url = "https://pro-api.solscan.io/v2.0/account/transfer"
    try:
        async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            status = resp.status
            text = await resp.text()
            marker = "✅" if status == 200 else ("❌" if status == 401 else "⚠️")
            print(f"{marker} {label:30s} -> statut {status}")
            if status == 200:
                print(f"      RÉPONSE (200 chars) : {text[:200]}")
            elif status != 401:
                print(f"      Détail : {text[:150]}")
    except Exception as e:
        print(f"❌ {label:30s} -> erreur : {e}")


async def main():
    if not config.SOLSCAN_API_KEY:
        print("❌ SOLSCAN_API_KEY vide dans .env — vérifie ton .env et config.py d'abord.")
        return

    key = config.SOLSCAN_API_KEY
    print(f"🔍 Test de {8} formats d'authentification différents contre l'API Solscan...\n")

    params = {
        "address": "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9",
        "token": config.SOL_MINT,
        "page": 1,
        "page_size": 10,
    }

    variants = [
        ("Header: token", {"token": key}),
        ("Header: Authorization (brut)", {"Authorization": key}),
        ("Header: Authorization Bearer", {"Authorization": f"Bearer {key}"}),
        ("Header: x-api-key", {"x-api-key": key}),
        ("Header: X-API-KEY (majuscules)", {"X-API-KEY": key}),
        ("Header: apikey", {"apikey": key}),
        ("Header: api-key", {"api-key": key}),
        ("Header: X-Solscan-Api-Key", {"X-Solscan-Api-Key": key}),
    ]

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=ThreadedResolver())) as session:
        for label, headers in variants:
            await try_variant(session, label, headers, params)

        # Variantes avec la clé en paramètre d'URL plutôt qu'en header
        print()
        for param_name in ["token", "apikey", "api_key", "key"]:
            params_with_key = dict(params)
            params_with_key[param_name] = key
            await try_variant(session, f"Query param: {param_name}", {}, params_with_key)

        # Test sur un endpoint BEAUCOUP plus simple (chaininfo, presque aucun
        # paramètre) — si ÇA marche mais pas account/transfer, le problème
        # n'est pas le format d'auth mais l'accès à cet endpoint précis.
        print("\n── Test sur un endpoint plus simple (chaininfo) ──")
        simple_url = "https://pro-api.solscan.io/v2.0/chaininfo"
        for label, headers in [("token", {"token": key}), ("Authorization Bearer", {"Authorization": f"Bearer {key}"})]:
            try:
                async with session.get(simple_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    text = await resp.text()
                    marker = "✅" if resp.status == 200 else "❌"
                    print(f"{marker} chaininfo avec {label:25s} -> statut {resp.status} — {text[:150]}")
            except Exception as e:
                print(f"❌ chaininfo avec {label:25s} -> erreur : {e}")

    print("\n── Interprétation ──")
    print("✅ = ce format fonctionne, à utiliser dans solscan_client.py")
    print("❌ = 401, mauvais format")
    print("⚠️ = ni 200 ni 401 — regarde le détail affiché (peut être 400/429/500, autre souci)")


if __name__ == "__main__":
    asyncio.run(main())
