# Guide de trading

Commencez en mode PAPER. Le bot détecte et analyse des wallets/tokens, applique des réglages par adresse et simule des positions. Les prix et historiques dépendent de fournisseurs tiers et peuvent être incomplets; les résultats simulés ne prédisent pas les résultats réels.

Les stops, prises de profit, filtres de risque et limites dépendent des données disponibles et ne protègent pas contre une liquidité disparue ou un token invendable. Vérifiez chaque configuration et les transactions avant tout usage avec fonds.

Le mode LIVE est une option risquée, jamais activée automatiquement. L'existence du code d'exécution ne constitue pas une validation de préparation à la production.

Les niveaux TP/SL et sorties pilotées par le bot sont surveillés localement; ils ne sont pas des ordres limite conservés on-chain. Si le processus ou le flux de prix est indisponible, le déclenchement peut être retardé. Le backtest doit être interprété selon [BACKTESTING.md](BACKTESTING.md): le replay OHLC n'est pas encore relié à tout le pipeline de sélection et n'intègre pas tous les coûts d'exécution Solana.
