# Backtesting

## Deux chemins présents

1. `historical_backtest.run_backtest` (également exposé par `backtest.replay_historical_ohlc`) est un moteur déterministe de replay OHLC. Il prend des chandelles fournies et des index de signaux, entre à l'ouverture de la chandelle suivante, applique frais et slippage exprimés en points de base, et choisit le stop-loss en cas d'ambiguïté TP/SL dans la même chandelle. Les entrées mal formées ou non finies lèvent `BacktestInputError`. Un signal au close de la bougie de sortie peut entrer à l'ouverture suivante.
2. `backtest.replay_ohlcv_path` rejoue chronologiquement les chandelles GeckoTerminal du chemin `backtest_token_pathaware`. Il prend la clôture de la première chandelle après le signal comme entrée et ignore sa mèche pour les sorties. Si les deux seuils apparaissent dans la même chandelle, il choisit le stop-loss.

Le moteur générique accepte `expected_interval_s` pour valider la grille temporelle et `allowed_missing_candles` pour autoriser explicitement un nombre borné de chandelles manquantes. Il renvoie alors `data_quality` avec l'intervalle, la plage temporelle et les gaps détectés. Si l'intervalle n'est pas fourni, il vérifie l'ordre des timestamps mais ne peut pas déduire les gaps. Le moteur GeckoTerminal historique continue de retourner un rendement brut de prix.

Aucun chemin n'intègre encore de façon complète les frais réseau/Jupiter, loyer, impact d'ordre, profondeur/liquidité et latence réels.

## Limites connues

- L'ancien `backtest_token` et certaines décisions `backtest_wallet` utilisent encore la variation DexScreener h24, qui ne reproduit pas une stratégie historique à entrées/sorties précises.
- Le replay OHLC déterministe n'est pas encore branché à tout le pipeline de sélection/monitoring du bot.
- Les datasets externes ne sont pas archivés/versionnés avec provenance, fuseau et politique de révision des bougies. La détection des gaps exige un `expected_interval_s` fourni par l'appelant.
- Un OHLC minute ne donne pas l'ordre à l'intérieur d'une bougie; le stop-first limite l'optimisme, mais ne reconstitue pas un fill réel.
- Aucun résultat de stratégie ne doit être présenté comme un profit réalisé ou une preuve d'avantage.

## Travail restant

Ajouter un dataset daté et sa provenance, brancher les signaux historiques réels sur le replay suivant leur horodatage, rapprocher frais/slippage avec la taille et la liquidité du marché, puis tester l'intégration complète sans accès aux bougies futures.
