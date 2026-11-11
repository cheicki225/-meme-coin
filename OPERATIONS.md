# Exploitation

1. Surveillez les logs, la disponibilité RPC et les erreurs des fournisseurs.
2. Sauvegardez `sniper_data.json` et `WALLET_ENCRYPTION_KEY` dans un emplacement privé distinct, puis testez la restauration.
3. À chaque redémarrage, inspectez les positions et réconciliez les signatures non résolues avant de reprendre les actions LIVE.
4. Gardez le mode PAPER tant que les tests et la surveillance ne couvrent pas les interruptions, les positions ouvertes et les confirmations on-chain.

La rotation de sauvegardes locale ne remplace pas une sauvegarde hors machine. Ne partagez jamais les logs avant de vérifier qu'ils ne contiennent pas de données sensibles.
