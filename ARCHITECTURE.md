# Architecture

- `main.py` orchestre la découverte, l'analyse et les composants du bot.
- `websocket_listener.py`, `protection_scanner.py` et `copytrade_listener.py` surveillent les événements et adresses.
- `fund_tracer.py`, `wallet_history.py`, `rug_scanner.py` et les clients Mobula/GMGN/Solscan collectent les signaux de risque et d'historique.
- `paper_trader.py` gère les positions; `live_trader.py` remplace les primitives d'exécution par `jupiter_executor.py`.
- `monitoring_list.py` persiste l'état dans un fichier JSON et conserve des sauvegardes locales.
- `telegram_bot.py` fournit la commande et l'interface utilisateur, protégée par l'allowlist Telegram.

Les données de prix, d'historique et de sécurité viennent d'APIs externes. Les données manquantes et défaillances réseau peuvent limiter les décisions; l'état JSON local n'est pas une base transactionnelle multi-processus.
