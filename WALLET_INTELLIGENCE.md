# Intelligence de wallets

## Implémenté

- Historique standard via `getSignaturesForAddress` et `getTransaction`, donc compatible avec le RPC HTTP public si le fournisseur autorise ces méthodes.
- Collecteur optionnel `WALLET_COLLECTOR_ENABLED=false`, en lecture seule, rond-point par wallet, borné par cycle et persisté dans une base SQLite séparée.
- Clé primaire signature/wallet/mint et curseur incrémental pour dédupliquer et reprendre après redémarrage.
- Une panne RPC n'est pas une liste d'activité vide dans le chemin strict du collecteur; une réponse vide est une activité vide valide.
- Les mouvements token/SOL sont stockés comme `candidate` quand le mouvement SOL est cohérent et autrement comme `unclassified_movement` avec confiance basse.

## Limites

La classification reste heuristique. Un transfert, wrap/unwrap, compte intermédiaire, frais ou agrégateur peut compliquer l'identification d'un achat/vente. Les champs de PnL réalisé restent vides dans les événements collectés; le collecteur ne prouve pas le coût d'acquisition historique ni ne déclenche seul une copie automatique. Les API Helius Enhanced et les WebSockets exigent la clé configurée par ce dépôt.

Voir `wallet_data_collector.py`, `wallet_history.py`, `wallet_collector_service.py` et les tests `tests/test_wallet_data_collector.py` / `tests/test_wallet_collector_service.py`.
