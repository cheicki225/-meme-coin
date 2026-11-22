# Exploitation

1. Gardez `TRADING_MODE=PAPER`; configurez `TELEGRAM_ALLOWED_USER_IDS` avant le lancement.
2. Sauvegardez le fichier SQLite indiqué par `SQLITE_DB_FILE` et `WALLET_ENCRYPTION_KEY` hors du serveur. La rotation locale ne remplace pas une sauvegarde hors machine.
3. Pour activer la collecte d'activité, laissez `WALLET_COLLECTOR_ENABLED=false` sauf si Helius est configuré. Le collecteur est en lecture seule; limitez le nombre de wallets et signatures par cycle.
4. Surveillez les alertes Telegram de santé RPC et du collecteur. Les erreurs de réseau ne doivent pas déclencher un retry de transaction.
5. Au démarrage, le bot recharge le document SQLite et examine les signatures pendantes. Une signature non attachée à une position maintient l'arrêt d'urgence; consultez la signature et l'état avant toute reprise.
6. `/halt` active l'arrêt d'urgence persistant. `/resume` n'est accepté qu'après résolution des signatures pendantes.
7. Ne testez pas Jupiter LIVE avec un wallet financé. L'intégration V2 et la restauration doivent être validées dans l'environnement cible avant toute décision LIVE.

Le backtest OHLC prend un jeu de chandelles réel fourni à `backtest.replay_historical_ohlc(candles, entry_signal_indices, ...)`. Les signaux sont exécutés à l'ouverture suivante; si TP et SL sont tous deux touchés dans une bougie, le SL est considéré en premier. Frais et slippage sont configurables et appliqués à l'entrée et à la sortie.

Ne partagez pas les journaux avant d'avoir vérifié qu'ils ne contiennent pas de données sensibles.
