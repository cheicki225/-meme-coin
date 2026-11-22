# Mission 6 — données historiques, frais et PnL

## Provenance et intégrité OHLC

`backtest_dataset.py` normalise les bougies et conserve source ID, mint, intervalle, date de collecte, schéma et SHA-256. Le chargement valide schéma, doublons et digest. Le digest prouve que le contenu correspond au digest inclus, sous réserve que le digest lui-même soit digne de confiance. La source et l'heure restent déclarées par l'appelant; le code ne conserve pas de reçu brut signé du fournisseur et ne vérifie pas une attestation distante. Aucun dataset historique réel avec provenance indépendante authentifiée n'est joint au dépôt.

Les tests `tests/test_backtest_dataset.py` vérifient stabilité du hash, modification de bougie, schéma, doublons JSON, intervalle et timestamps des signaux. Ils vérifient l'intégrité du fichier, pas l'authenticité de sa source.

## Coûts réels et modèle de backtest

- Le replay OHLC applique un modèle générique en points de base pour entrées/sorties et un slippage configuré. Une bougie ne contient pas la séquence intrabar complète; la règle prudente donne priorité au stop quand TP et SL sont touchés dans la même bougie.
- Le modèle ne reconstitue pas les frais de priorité par slot, coût de route Jupiter, loyer/rent exact, taxe de token, impact complet de la liquidité, frais de transaction échouée, ni MEV.
- La comptabilité de swaps confirmés utilise les deltas de soldes et frais lus dans les métadonnées confirmées. La conversion USD utilise un taux disponible après confirmation, pas une cotation historique authentifiée pour le slot.
- Les frais d'une transaction finalisée en échec sont conservés séparément en lamports. Ils ne sont pas convertis automatiquement en USD ni déduits de tout l'historique PnL.
- PAPER utilise des frais simulés et un taux simulé/actuel. Le benchmark Mission 6 impose arbitrairement 100 USD/SOL; ce n'est pas un taux de marché.

## Conclusion

Le backtest permet une comparaison reproductible de stratégie sur un même fichier et prend en compte des coûts paramétriques. Il ne fournit pas encore un PnL historique réel net de tous les coûts Solana, ni des données OHLC dont la provenance est prouvée indépendamment. Les rendements sont indicatifs tant que l'ingestion ne conserve pas la réponse brute, son empreinte et horodatage vérifiables, ainsi qu'un historique SOL/USD cohérent au slot.
