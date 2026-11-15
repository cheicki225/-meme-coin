# Backtesting

## Deux chemins présents

1. `historical_backtest.run_backtest` (également exposé par `backtest.replay_historical_ohlc`) est un moteur déterministe de replay OHLC. Il prend des chandelles fournies et des index de signaux, entre à l'ouverture de la chandelle suivante, applique frais et slippage exprimés en points de base, et choisit le stop-loss en cas d'ambiguïté TP/SL dans la même chandelle. Les entrées mal formées ou non finies lèvent `BacktestInputError`.
2. `backtest.replay_ohlcv_path` rejoue chronologiquement les chandelles GeckoTerminal du chemin `backtest_token_pathaware`. Il prend la clôture de la première chandelle après le signal comme entrée et ignore sa mèche pour les sorties. Si les deux seuils apparaissent dans la même chandelle, il choisit le stop-loss.

Les deux chemins ont des frais différents : le premier prend des frais/slippage génériques configurés, le second retourne un rendement brut de prix. Aucun n'intègre encore de façon complète les frais réseau/Jupiter, loyer, impact d'ordre, profondeur/liquidité et latence réels.

## Limites connues

- L'ancien `backtest_token` et certaines décisions `backtest_wallet` utilisent encore la variation DexScreener h24, qui ne reproduit pas une stratégie historique à entrées/sorties précises.
- Le replay OHLC déterministe n'est pas encore branché à tout le pipeline de sélection/monitoring du bot.
- Les datasets externes ne sont pas archivés/versionnés avec provenance, trous, fuseau et politique de révision des bougies.
- Un OHLC minute ne donne pas l'ordre à l'intérieur d'une bougie; le stop-first limite l'optimisme, mais ne reconstitue pas un fill réel.
- Aucun résultat de stratégie ne doit être présenté comme un profit réalisé ou une preuve d'avantage.

## Travail restant

Ajouter un dataset daté et sa provenance, détecter les gaps, convertir les signaux d'entrée réels en replay suivant l'horodatage, rapprocher frais/slippage avec la taille et liquidité du marché, puis tester l'intégration complète sans accès aux bougies futures.
