---
name: backtesting-validation
description: "Use when reviewing historical OHLC replay, backtest datasets, signal timestamps, costs, gaps, drawdown, or lookahead controls."
---

# Backtesting et validation historique

## Déclencheurs

Active ce Skill pour préparer, auditer ou modifier les replays historiques, les datasets OHLC versionnés, les signaux indexés/horodatés, les frais, le slippage, les gaps, le drawdown ou une comparaison de stratégies. Pour un score de wallet basé sur un résumé h24, vérifie d'abord s'il s'agit du chemin historique heuristique et pas d'un replay.

## Audit avant toute modification

- Vérifie branche, stack et diff; lis `BACKTESTING.md`, `TESTING.md`, `historical_backtest.py`, `backtest_dataset.py`, les wrappers dans `backtest.py` et `tests/test_historical_backtest.py`, `tests/test_backtest_dataset.py`, `tests/test_backtest_ohlcv_replay.py`.
- Identifie le moteur utilisé: `historical_backtest.run_backtest` sur chandelles fournies et `backtest.replay_ohlcv_path` (GeckoTerminal) ont des conventions d'entrée différentes.
- Vérifie la source déclarée, le fuseau et l'intervalle du dataset, l'horodatage du signal, la politique des chandelles manquantes dans le manifeste, les frais/slippage et les données qui n'étaient pas connues au moment du signal.

## Workflow

1. Utilise des données historiques explicites et des tests offline; ne récupère pas prix ou transactions live dans le moteur déterministe.
2. Pour `run_backtest`, confirme que le signal de clôture entre à l'ouverture de la bougie suivante, que les paramètres sont finis/valides et que l'ambiguïté stop/take-profit reste traitée de façon conservatrice (stop d'abord).
3. Pour un dataset, passe par `backtest_dataset.validate_dataset`/`backtest.replay_historical_dataset`, lie intervalle et politique des gaps au manifeste et vérifie le SHA-256 avant d'associer les signaux à des timestamps exacts.
4. Rapporte les gaps et trades affectés sans inventer de prix manquants. Compare les horodatages à l'intervalle et à la politique de chandelles manquantes du dataset; le hash ne prouve ni la provenance ni l'authenticité du marché.
5. Toute amélioration de performance doit inclure un cas sans lookahead, une hypothèse de coût/slippage et une comparaison reproductible sur le même dataset.

## Sécurité

Aucun ordre, signature ou RPC n'est nécessaire pour un replay local; n'en exécute pas. Ne modifie pas les règles de risque pour améliorer le résultat. N'expose pas de données utilisateur ou d'identifiants de provider; le champ `source` du dataset doit être un identifiant, pas une URL contenant un token. Aucun résultat ne déclenche LIVE.

## Tests

Utilise `tests/test_historical_backtest.py`, `tests/test_backtest_dataset.py`, `tests/test_backtest_ohlcv_replay.py` et `tests/test_historical_backtest_integration.py`. Couvre les prix non finis, signaux hors archive, digest altéré, timestamps décalés, gaps refusés/autorisés explicitement, coût des deux côtés et barre ambiguë. Les tests doivent être entièrement hors ligne.

## Preuves attendues

Archive le dataset exact et son manifeste/digest avec les paramètres et la version de code; rapporte couverture temporelle, source annoncée, hypothèses, trades, frais, drawdown, gaps et mapping signal→bougie. Sépare résultat du replay, test synthétique et performance opérationnelle. Un digest valide prouve seulement l'intégrité par rapport au manifeste conservé.

## Limites

Aucun dataset externe authentifié ni récupérateur automatique n'est fourni. Les stratégies de sélection du bot ne sont pas intégrées complètement au replay. Les barres OHLC ne révèlent pas l'ordre exact intrabougie; le stop-first est une convention conservatrice, pas un fill réel. Certains anciens chemins `backtest_token`/`backtest_wallet` utilisent un rendement h24 heuristique. Les résultats ne prouvent pas un avantage ni un profit futur.

## Passages de relais

- Calcul du PnL/frais de transactions: `pnl-accounting`.
- Paramètres de stop, exposition ou circuit breaker: `trading-risk-manager`.
- Collecte d'historique et provenance RPC: `wallet-intelligence-collector` ou `solana-rpc-performance` seulement si une collecte est demandée.
- Benchmark comparatif reproductible: `f-project-benchmark`.