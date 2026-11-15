# Journal des modifications

## Non publié — correction de fiabilité et audit (15 novembre 2026)

- Refuse les métadonnées Jupiter booléennes, flottantes, tronquées ou sans `gasless=false`; les erreurs HTTP Jupiter ne recopient plus le corps de réponse.
- Remplace le retry RPC linéaire par une politique limitée qui respecte `Retry-After`, applique backoff/jitter et n'écrit ni corps de fournisseur ni texte d'exception sensible dans les logs.
- Permet au mode PAPER et au collecteur de wallets d'utiliser le RPC HTTP public; différencie panne RPC et historique vide, et bloque le démarrage LIVE lorsque les capacités Helius requises ne sont pas configurées.
- Sanitize les exceptions provenant des clients externes et Telegram afin de ne pas journaliser les URL qui peuvent embarquer des clés.
- Durcit les nombres de comptabilité on-chain, réutilise le flux SOL confirmé au lieu de refaire une conversion, et bloque des mutations PAPER héritées en LIVE.
- Rejoue les chandelles OHLC chronologiquement avec stop-first en cas d'ambiguïté; durcit la validation des données et paramètres de backtest.
- Ajoute les statuts et limites vérifiés dans `PROGRESS.md`, `PROJECT_ROADMAP.md`, `RPC_PUBLIC_LIMITATIONS.md` et les guides spécialisés. Aucun swap réel exécuté.

## Non publié — phase de fiabilité (11 novembre 2026)

- Revue des 16 fichiers modifiés dans la PR n°3; migration de l'exécuteur vers les endpoints Jupiter Swap API V2 officiels.
- Verrouille LIVE par défaut derrière un opt-in explicite; résout les Address Lookup Tables et valide les ATA, payeurs, signataires, programmes top-level, blockhash, frais et loyers.
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
