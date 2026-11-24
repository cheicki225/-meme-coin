# Backtesting

## Deux chemins présents

1. `historical_backtest.run_backtest` (également exposé par `backtest.replay_historical_ohlc`) est un moteur déterministe de replay OHLC. Il prend des chandelles fournies et des index de signaux, entre à l'ouverture de la chandelle suivante, applique frais et slippage exprimés en points de base, et choisit le stop-loss en cas d'ambiguïté TP/SL dans la même chandelle. Les entrées mal formées ou non finies lèvent `BacktestInputError`. Un signal au close de la bougie de sortie peut entrer à l'ouverture suivante.
2. `backtest.replay_ohlcv_path` rejoue chronologiquement les chandelles GeckoTerminal du chemin `backtest_token_pathaware`. Il prend la clôture de la première chandelle après le signal comme entrée et ignore sa mèche pour les sorties. Si les deux seuils apparaissent dans la même chandelle, il choisit le stop-loss.

Le moteur générique accepte `expected_interval_s` pour valider la grille temporelle et `allowed_missing_candles` pour autoriser explicitement un nombre borné de chandelles manquantes. Il renvoie alors `data_quality` avec l'intervalle, la plage temporelle et les gaps détectés. Si l'intervalle n'est pas fourni, il vérifie l'ordre des timestamps mais ne peut pas déduire les gaps. Le moteur GeckoTerminal historique continue de retourner un rendement brut de prix.

`run_backtest` renvoie aussi `valuation_scope` pour que le résultat ne soit pas confondu avec un PnL réalisé : les prix sont supposés être en USD par l'interface `position_usd`, mais le moteur brut ne reçoit ni devise de cotation ni source de prix; des secondes Unix sont attendues, mais l'échelle des timestamps n'est pas vérifiée; les frais et le slippage sont un scénario fixe en points de base. Ce scénario exclut les frais réseau Solana, les frais prioritaires, le loyer, les tips et les coûts fournisseur. `replay_historical_dataset` ajoute le manifeste déclaré et son SHA-256; le hash détecte les modifications du contenu par rapport au manifeste, mais n'authentifie ni le fournisseur ni les prix.

## Dataset versionné

`backtest_dataset.py` fournit un format JSON versionné avec `asset_id`, identifiant de source, intervalle, date de collecte, chandelles OHLC normalisées et digest SHA-256. `save_dataset` écrit atomiquement, `load_dataset` refuse les constantes JSON non standard/duplicatas, et `backtest.replay_historical_dataset` valide le digest avant replay puis attache la provenance au résultat. L'intervalle et la politique des gaps sont liés au dataset, pas choisis séparément au moment du replay. Les signaux peuvent être fournis sous forme d'horodatages Unix exacts d'une bougie; le replay les associe à cette clôture puis entre à l'ouverture de la suivante. Un timestamp absent de l'archive est rejeté.

Le digest détecte une modification accidentelle relativement au manifeste conservé; il ne certifie pas l'authenticité de la source. Aucun récupérateur automatique ni dataset réel vérifié n'est fourni, et il faut conserver les fichiers de dataset avec leurs résultats pour permettre une reproduction ultérieure.

Aucun chemin n'intègre encore de façon complète les frais réseau/Jupiter, loyer, impact d'ordre, profondeur/liquidité et latence réels.

## Limites connues

- L'ancien `backtest_token` et certaines décisions `backtest_wallet` utilisent encore la variation DexScreener h24, qui ne reproduit pas une stratégie historique à entrées/sorties précises.
- Le replay OHLC déterministe n'est pas encore branché à tout le pipeline de sélection/monitoring du bot.
- Le format versionné et le hash sont implémentés, mais aucun dataset externe réel n'est archivé par le dépôt; authenticité, fuseau, révisions et qualité amont restent à vérifier.
- Un OHLC minute ne donne pas l'ordre à l'intérieur d'une bougie; le stop-first limite l'optimisme, mais ne reconstitue pas un fill réel.
- Aucun résultat de stratégie ne doit être présenté comme un profit réalisé ou une preuve d'avantage.

## Travail restant

Brancher l'ingestion d'un historique fiable et les signaux de sélection réellement produits par le bot sur le replay horodaté, rapprocher frais/slippage avec taille/liquidité, puis tester l'intégration complète sans accès aux bougies futures.
