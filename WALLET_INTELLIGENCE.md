# Intelligence de wallets

## Implémenté

- Historique standard via `getSignaturesForAddress` et `getTransaction`, donc compatible avec le RPC HTTP public si le fournisseur autorise ces méthodes.
- Collecteur optionnel `WALLET_COLLECTOR_ENABLED=false`, en lecture seule, rond-point par wallet, borné par cycle et persisté dans une base SQLite séparée.
- Clé primaire signature/wallet/mint et curseur incrémental pour dédupliquer et reprendre après redémarrage.
- Une panne RPC n'est pas une liste d'activité vide dans le chemin strict du collecteur; une réponse vide est une activité vide valide.
- Les mouvements token/SOL sont stockés comme `candidate` quand le mouvement SOL est cohérent et autrement comme `unclassified_movement` avec confiance basse.
- Chaque collecte indique sa source de normalisation (`getTransaction` + deltas de soldes), sa portée bornée à une page de signatures et un état de complétude de cette page. `current_page_processed` ne signifie pas que tout l'historique du wallet est complet; `empty_page` rapporte uniquement que le fournisseur a renvoyé une page vide.
- `collected_at` est l'heure locale observée juste avant la tentative d'écriture, tandis que `block_time` vient des métadonnées de la transaction et peut être absent. Ce sont des repères de provenance/fraîcheur, sans garantie de fraîcheur ni d'exhaustivité du fournisseur. Le champ `source` décrit la méthode de normalisation et n'identifie pas le fournisseur ou l'endpoint configuré.
- Une ligne de signature mal formée rend le résultat `partial` et empêche l'avancement du curseur. Une page qui dépasse la limite demandée est refusée sans traiter ses transactions. Une panne de récupération reste `unknown`; elle ne devient pas une liste d'activité vide.
- Une réponse dont les instructions de transaction de premier niveau ou imbriquées sont absentes, mal formées, ou portent un `programId` qui n'est pas une clé publique Solana base58 de 32 octets rend la page `partial` et laisse le curseur inchangé, car une invocation Pump.fun ne peut pas être exclue. Pour une transaction Pump.fun, une réponse `meta` absente, des tableaux `preTokenBalances`/`postTokenBalances` nuls ou mal formés, ou une ligne dont le propriétaire/solde est indéterminé ont le même traitement. Les quantités dépassant `u64` sont aussi rejetées comme indéterminées. Le schéma RPC Solana autorise des métadonnées et des tableaux de soldes indisponibles; le compte SPL stocke ses quantités en `u64` : [structures JSON RPC Solana](https://solana.com/docs/rpc/json-structures), [compte de jeton SPL](https://solana.com/docs/tokens/basics/create-token-account).

## Limites

La classification reste heuristique. Un transfert, wrap/unwrap, compte intermédiaire, frais ou agrégateur peut compliquer l'identification d'un achat/vente. Les champs de PnL réalisé restent vides dans les événements collectés; le collecteur ne prouve pas le coût d'acquisition historique ni ne déclenche seul une copie automatique. Les API Helius Enhanced et les WebSockets exigent la clé configurée par ce dépôt.

Voir `wallet_data_collector.py`, `wallet_history.py`, `wallet_collector_service.py` et les tests `tests/test_wallet_data_collector.py` / `tests/test_wallet_collector_service.py`.
