# Journal des modifications

## Non publié — comptabilité PAPER et qualité OHLC (22 novembre 2026)

- Déduit les frais simulés des achats PAPER, des ajouts DCA et des ventes partielles/totales; les frais d'achat sont ajoutés au coût de revient et les frais de vente au PnL réalisé.
- Fait passer Sell Initials par la vente simulée, comptabilise son PnL et son cashflow SOL net de frais, tout en gardant la méthode bloquée en LIVE.
- Autorise un signal au close de la bougie de sortie à entrer à l'ouverture suivante; ajoute une validation explicite de l'intervalle des chandelles et un rapport de gaps lorsque l'intervalle est connu.
- Durcit la reprise SQLite contre les erreurs transitoires et états JSON/signatures mal formés; renvoie le résultat de livraison Telegram et retente les alertes RPC non livrées.
- Renforce les contrôles d'exposition sur les états de risque non valides et teste que l'arrêt d'urgence bloque les swaps LIVE d'achat et de vente sans liquidation automatique. Aucun swap réel exécuté.
- Lie le minimum de sortie Jupiter à la limite réellement encodée dans la route et plafonne les configurations de slippage à 10 % et d'impact à 5 %.
- Parse les contrôles GoPlus Solana et refuse les résultats incomplets, parts LP incohérentes et TVL DEX rapportées sous le seuil configurable (10 000 USD par défaut lorsque le filtre est actif).
- Signale les PnL hérités dont les frais ne peuvent être reconstruits; le marquage persistant n'altère pas les totaux ni les anciennes positions.
- Ajoute un format OHLC JSON versionné/hashé, la correspondance de signaux par timestamps exacts et les gaps traversés par les trades.
- Augmente la suite locale à 318 tests réussis; la CI #202 passe compilation, mode PAPER, tests, Ruff, pip-audit et Gitleaks.

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
