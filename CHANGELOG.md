# Journal des modifications

## Non publié — phase de fiabilité (11 novembre 2026)

- Revue des 16 fichiers modifiés dans la PR n°3; migration de l'exécuteur vers les endpoints Jupiter Swap API V2 officiels.
- Rejette les routeurs autres que Metis, les payeurs tiers, les transactions sponsorisées, les frais/loyers au-delà des limites, les programmes top-level inattendus et les blockhash invalides.
- Persiste la signature avant exécution, utilise `/execute` sans renvoi automatique et attend `finalized` avant de calculer le PnL.
- Calcule les flux SOL/token à partir des soldes confirmés, frais et loyer inclus; verrouille les ventes LIVE MultiBuy tant que la comptabilité par wallet manque.
- Ajoute SQLite WAL, sauvegardes cohérentes et restauration, import JSON initial et protection du numéro de schéma.
- Intègre le collecteur de wallets incrémental en lecture seule, les alertes de santé RPC/collecteur et les commandes Telegram `/halt` et `/resume`.
- Applique des plafonds de positions/exposition, réserve le budget contre les achats concurrents, bloque les transferts manuels en PAPER et désactive les bundles Jito à tip séparé.
- Ajoute un replay OHLC avec frais/slippage, contrôles de pertes, comptabilité de transaction, reprise, collecteur et surveillance.
- Avancement estimé documenté : 78 %. Tests d'intégration on-chain, source OHLC et soak PAPER restent ouverts.

## Audit initial de la PR n°3

- Ajout de limites de sécurité sur les devis, transactions Jupiter, blockhash et frais.
- Protection des commandes Telegram par une allowlist d'identifiants privés.
- Documentation de configuration, exploitation, sécurité et limites du bot.
- Ajout d'une suite de tests de validation et d'un workflow CI.
