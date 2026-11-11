# Architecture

- `main.py` orchestre la découverte, le suivi et les services d'arrière-plan.
- `websocket_listener.py`, `protection_scanner.py` et `copytrade_listener.py` suivent les événements et adresses. `wallet_collector_service.py` ajoute une collecte optionnelle en lecture seule et incrémentale.
- `fund_tracer.py`, `wallet_history.py`, `rug_scanner.py` et les clients Mobula/GMGN/Solscan fournissent les signaux de risque et d'historique.
- `paper_trader.py` applique les contrôles globaux d'exposition et le flux PAPER; `live_trader.py` ajoute le portefeuille d'exécution et délègue les swaps à `jupiter_executor.py`.
- `jupiter_executor.py` demande un ordre à Jupiter Swap API V2, restreint les routeurs, valide le message, persiste la signature, signe localement, utilise l'inclusion gérée V2, attend le statut finalized puis calcule les deltas depuis les métadonnées RPC.
- `transaction_accounting.py` convertit les soldes SOL et tokens confirmés en flux de trésorerie sans remplacer les fills par les estimations du devis.
- `monitoring_list.py` garde un document d'état atomique dans SQLite WAL, importe l'ancien JSON et restaure les sauvegardes. Les signatures non résolues conservent l'arrêt d'urgence après redémarrage.
- `rpc_client.py` conserve un pool HTTP partagé, mesure erreurs/latences et expose des alertes; `notifier.py` envoie les alertes risque, RPC et collecteur sur Telegram.
- `historical_backtest.py` relit des chandelles OHLC fournies, avec exécution à la bougie suivante, frais, slippage et règle prudente en cas de bougie ambiguë. Il n'invente pas de données historiques.
- `telegram_bot.py` fournit l'interface, protégée par une allowlist privée, ainsi que `/halt` et `/resume`.

Les services RPC, historiques, prix et sécurité reposent sur des fournisseurs externes. Les erreurs bloquent les opérations risquées; elles ne prouvent pas que les données externes sont exactes. Le mode PAPER reste la configuration par défaut et le code LIVE demeure non validé.
