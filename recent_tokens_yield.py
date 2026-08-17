"""
════════════════════════════════════════════════════════════════
RECENT TOKENS YIELD — Capture N tokens récents + backtest à seuil fixe
════════════════════════════════════════════════════════════════
Se connecte brièvement au WebSocket Pump.fun (comme websocket_listener.py,
réutilisé tel quel — même détection que le bot en LIVE), capture les N
prochains tokens créés, puis backteste chacun avec le seuil de TP demandé
et le SL RÉELLEMENT CONFIGURÉ dans ton bot (config.SL_PCT) — pas un chiffre
inventé, les vraies règles que tu as mises en place.

⚠️ LIMITE IMPORTANTE : un token tout juste créé (quelques secondes) n'a
souvent AUCUNE donnée de prix sur DexScreener pour l'instant — rien à
backtester. Ce script attend un peu (WAIT_BEFORE_BACKTEST_S) après avoir
capturé chaque token avant de tenter son backtest, pour laisser une chance
à DexScreener de l'indexer. Même avec ça, certains tokens très récents
peuvent quand même n'avoir aucune donnée exploitable — c'est affiché
clairement, pas caché.

Usage :
    python recent_tokens_yield.py [--count 10] [--tp 100]

Ne fait AUCUN achat, AUCUNE modification du monitoring — lecture seule,
comme find_dev.py et les autres outils d'analyse.
"""

import argparse
import asyncio
import sys
import time

import config
import websocket_listener
import backtest

WAIT_BEFORE_BACKTEST_S = 90  # laisse le temps à DexScreener d'indexer avant de tenter le backtest


async def capture_recent_tokens(count: int, timeout_s: int = 300) -> list:
    """
    Capture les `count` prochains tokens créés sur Pump.fun, via le même
    WebSocket que le bot utilise en LIVE. S'arrête dès que `count` tokens
    sont trouvés, ou après `timeout_s` secondes (par sécurité, si le
    marché est anormalement calme).
    """
    captured = []
    done_event = asyncio.Event()

    async def on_new_token(token_info: dict):
        captured.append({
            "token_mint": token_info["token_mint"],
            "dev_address": token_info["dev_address"],
            "captured_at": time.time(),
        })
        print(f"   [{len(captured)}/{count}] Capturé : {token_info['token_mint']}")
        if len(captured) >= count:
            done_event.set()

    listener = websocket_listener.NewTokenListener(on_new_token)
    listen_task = asyncio.create_task(listener.start())

    try:
        await asyncio.wait_for(done_event.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        print(f"   ⏱️  Timeout après {timeout_s}s — seulement {len(captured)}/{count} tokens capturés.")
    finally:
        await listener.stop()
        listen_task.cancel()
        try:
            await listen_task
        except asyncio.CancelledError:
            pass

    return captured


async def main():
    parser = argparse.ArgumentParser(description="Capture N tokens récents et backteste avec les règles du bot.")
    parser.add_argument("--count", type=int, default=10, help="Nombre de tokens à capturer (défaut: 10)")
    parser.add_argument("--tp", type=float, default=100.0, help="Seuil de Take Profit à tester en %% (défaut: 100)")
    parser.add_argument("--timeout", type=int, default=300, help="Timeout de capture en secondes (défaut: 300)")
    args = parser.parse_args()

    if not config.HELIUS_API_KEY:
        print("❌ Aucune clé HELIUS_API_KEY configurée dans .env.")
        sys.exit(1)

    print(f"🔍 Capture des {args.count} prochains tokens créés sur Pump.fun...")
    print(f"   (connexion WebSocket réelle, identique à celle du bot en LIVE)\n")

    tokens = await capture_recent_tokens(args.count, timeout_s=args.timeout)

    if not tokens:
        print("\n❌ Aucun token capturé — vérifie ta connexion/clé Helius.")
        return

    print(f"\n✅ {len(tokens)} token(s) capturé(s). Attente de {WAIT_BEFORE_BACKTEST_S}s avant le backtest")
    print(f"   (laisse le temps à DexScreener d'indexer les tokens les plus récents)...")
    await asyncio.sleep(WAIT_BEFORE_BACKTEST_S)

    print(f"\n📊 Backtest de chaque token — TP testé : +{args.tp:.0f}% | SL du bot : -{config.SL_PCT:.0f}%\n")

    results = []
    no_data_count = 0
    for t in tokens:
        result = await backtest.backtest_token(t["token_mint"], tp_pct=args.tp, sl_pct=config.SL_PCT)
        if result is None or result.get("result_pct") is None:
            print(f"   ⚠️  {t['token_mint'][:12]}...  →  pas de données exploitables (trop récent pour DexScreener)")
            no_data_count += 1
            continue
        results.append(result)
        status_icon = "✅" if result.get("hit_tp") else ("❌" if result.get("hit_sl") else "➖")
        print(f"   {status_icon} {t['token_mint'][:12]}...  →  {result['result_pct']:+7.1f}%  ({result.get('reason', '?')})")

    if not results:
        print("\n❌ Aucun des tokens capturés n'a de données exploitables — trop récents.")
        print("   Réessaie avec --timeout plus grand pour capturer des tokens un peu plus espacés,")
        print("   ou relance ce script plus tard pour laisser plus de temps à l'indexation.")
        return

    total_pct = sum(r["result_pct"] for r in results)
    avg_pct = total_pct / len(results)
    wins = [r for r in results if r["result_pct"] > 0]
    win_rate = len(wins) / len(results) * 100

    stake = 100
    final_value = sum(stake * (1 + r["result_pct"] / 100) for r in results)
    total_invested = stake * len(results)
    net_result = final_value - total_invested

    print(f"\n{'='*60}")
    print(f"📈 RÉSULTATS ({len(results)}/{len(tokens)} tokens avec données exploitables, {no_data_count} ignorés)")
    print(f"{'='*60}")
    print(f"Résultat cumulé      : {total_pct:+.1f}%")
    print(f"Résultat moyen/trade  : {avg_pct:+.1f}%")
    print(f"Win rate              : {win_rate:.0f}%")
    print(f"\n💰 Simulation ({stake}$ par token, {len(results)} trades) :")
    print(f"   Investi : {total_invested:.0f}$  →  Final : {final_value:.0f}$  ({net_result:+.0f}$)")

    if net_result > 0:
        print(f"\n✅ VERDICT : rentable sur cet échantillon, avec TP +{args.tp:.0f}% / SL -{config.SL_PCT:.0f}%.")
    else:
        print(f"\n❌ VERDICT : PAS rentable sur cet échantillon.")

    print(f"\n⚠️  Rappel : échantillon de {len(results)} tokens pris au hasard sur le marché (pas des")
    print(f"   ruggers pré-qualifiés par ton bot) — un résultat très différent de ce que ton bot")
    print(f"   obtiendrait en pratique, puisqu'il filtre les devs avant d'acheter, pas au hasard.")


if __name__ == "__main__":
    asyncio.run(main())
