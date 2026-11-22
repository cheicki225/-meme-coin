# Sécurité et statut de l'exécution

## Mode de fonctionnement

Gardez `TRADING_MODE=PAPER` et `LIVE_EXECUTION_ENABLED=false`. Le code LIVE exige ces deux opt-ins simultanément, mais il n'a pas été validé avec un RPC de production, un portefeuille de test, ni un soak test. Aucun swap réel n'a été exécuté au cours de cette mise à jour.

## Garde-fous implémentés

- Jupiter Swap API V2 utilise les endpoints officiels fixes `/swap/v2/order` et `/swap/v2/execute`, avec `JUPITER_API_KEY`. Les routeurs JupiterZ, Dflow et OKX sont exclus; seule une route Metis est acceptée.
- Avant signature, l'exécuteur résout les Address Lookup Tables depuis le RPC, puis vérifie les mints et montants, le minimum reçu exactement égal au plancher encodé par la transaction, l'impact, le wallet taker/payeur, le nombre de signataires, les programmes top-level, les ATA du wallet, le blockhash, les frais réseau et le loyer de comptes estimé. Pour Jupiter, seules les instructions de route exact-in V6 prises en charge sont acceptées; leurs mints, ATA, montants, sortie cotée et slippage sont comparés au devis. Le slippage est plafonné à 10 % et l'impact de prix à 5 %, même si l'environnement configure une valeur supérieure. Les instructions manquantes, tronquées ou d'un format inconnu échouent fermé.
- La signature est créée localement. L'API reçoit la transaction signée pour l'inclusion; la clé privée n'est jamais envoyée.
- La signature est persistée avant l'appel d'exécution. Les issues incertaines ne sont jamais renvoyées automatiquement et activent l'arrêt d'urgence.
- Une vente/position n'est comptabilisée qu'après confirmation `finalized` et lecture du `getTransaction`. Le PnL SOL inclut les frais réseau et le loyer réellement reflétés dans les soldes du wallet. La conversion USD utilise le cours SOL récupéré au moment du trade; elle n'est pas une valorisation historique de marché.
- Pour un swap exact-in dépensant un token non natif, le débit confirmé doit égaler le montant brut demandé. Tout écart laisse la signature en attente et bloque la comptabilisation; les tokens à frais de transfert ne sont pas pris en charge. Un input SOL est traité séparément par les deltas natifs incluant frais et loyer.
- Les décimales de tokens sont prises dans les métadonnées confirmées. Les positions LIVE MultiBuy sont refusées jusqu'à une comptabilisation par wallet.
- Les transferts SOL manuels sont bloqués en PAPER, limités par montant en LIVE et refusés pendant l'arrêt d'urgence ou une transaction pendante. Les bundles Jito avec transfert de tip séparé restent désactivés.
- L'état du bot est stocké en SQLite WAL avec commits atomiques, import initial depuis l'ancien JSON, sauvegardes et restauration depuis un instantané valide. Une version de schéma plus récente n'est jamais rétrogradée.
- `/halt` déclenche l'arrêt d'urgence avant de sauvegarder. Si la sauvegarde échoue, le bot reste arrêté en mémoire et Telegram indique que cet état ne survivra pas forcément à un redémarrage. `/resume` refuse un registre de signatures mal formé ou non vide. La limite de positions et d'exposition globale est vérifiée avant un nouvel achat et protégée contre les entrées concurrentes.
- Le collecteur de wallets et le moniteur RPC sont en lecture seule; les résultats partiels et erreurs persistantes du curseur génèrent des alertes Telegram avec cooldown. `/activity <wallet surveillé>` affiche au plus 10 événements locaux, sans générer d'ordre ni de PnL.
- Les parseurs numériques rejettent booléens, flottants et formats mal formés pour les montants/frais Jupiter et les deltas de comptabilité. Les outils réseau ne journalisent plus le corps brut ni les messages d'exception contenant potentiellement une URL avec clé.
- Le filtre GoPlus utilise le schéma Solana (`mintable`, `freezable`, `closable`, contrôles Token-2022, DEX et détenteurs LP); il ne présente pas les champs EVM de taxe/honeypot comme s'ils avaient été évalués. Un état obligatoire absent, un échec fournisseur, une liquidité DEX absente, un verrouillage LP inconnu ou des parts LP incohérentes bloque l'achat lorsque le filtre est activé. Par défaut, le maximum de TVL DEX rapporté doit atteindre 10 000 USD (`GOPLUS_MIN_DEX_TVL_USD`); ce chiffre ne prouve pas que la route Jupiter utilise cette pool. Le score est heuristique : les lignes LP retournées ne couvrent pas forcément tout le pool; moins de 25 % verrouillé retranche 45 points, 25–<50 % 30 points, 50–<80 % 15 points, et 80 % ou plus aucune pénalité. Même une note élevée n'est ni un audit ni une garantie de vente.
- En absence de clé Helius, le bot PAPER démarre avec HTTP RPC standard en mode dégradé; le démarrage LIVE est bloqué. Le fournisseur configuré au déploiement reste inconnu.
- Les méthodes `Buy the Dip` et `Sell Initials` héritées du simulateur sont bloquées en LIVE tant qu'elles ne passent pas par un swap réel confirmé.

## Limites avant toute activation LIVE

1. Les routes exact-in Jupiter reconnues sont liées aux champs contrôlés du devis, mais le route plan et chaque instruction CPI/DEX ne sont pas audités programme par programme. La validation top-level ne prouve pas à elle seule l'innocuité de tout programme invoqué on-chain; les nouveaux formats Jupiter sont rejetés jusqu'à prise en charge explicite.
2. Les signatures pendantes non attachées à une position mettent le bot en arrêt d'urgence. Leur réconciliation après redémarrage reste manuelle; aucun retry ni reconstruction approximative de position n'est effectué.
3. L'intégration Jupiter V2 n'a pas été exécutée sur devnet/mainnet. Les tests sont mockés et ne prouvent ni la disponibilité des routes ni le comportement du RPC cible.
4. Le calcul USD utilise un cours SOL instantané au moment de l'exécution; les historiques de prix complets, les frais de fournisseur et les écarts de valeur SOL/USD entre entrée et sortie ne sont pas reconstruits.
5. Le replay OHLC déterministe et le chemin GeckoTerminal existent, mais le moteur OHLC n'est pas relié à tout le pipeline de sélection; coûts Solana/Jupiter, provenance, trous de données et scoring wallet demeurent incomplets/heuristiques.
6. SQLite garantit l'atomicité du document d'état pour un processus du bot; aucun verrouillage multi-processus ni sauvegarde hors machine n'est fourni.
7. Les fonctionnalités existantes de copy trading, de Buy on Dev Sell et de détection de régularité conservent les limites documentées dans README. Le collecteur de wallets n'alimente pas directement les entrées de trade; ses événements restent des données de suivi séparées.

## Validation de sécurité requise

Avant toute décision LIVE, la CI de la branche doit rester verte (run [45059131294](https://github.com/cheicki225/-meme-coin/actions/runs/45059131294) réussi sur `a30f11a`), tester les réponses Jupiter adversariales, exécuter un soak PAPER dans l'environnement cible, vérifier la restauration et la reprise, puis faire une revue indépendante des instructions on-chain et du PnL. N'utilisez pas un wallet qui contient des fonds à long terme.

## Revue du 22 novembre 2026

- Les appels Jupiter `/swap/v2/order` et `/swap/v2/execute` refusent maintenant les redirections HTTP (`allow_redirects=False`) afin de ne pas transmettre `x-api-key` ou une transaction signée à une destination redirigée.
- `/reconcile` active l'arrêt d'urgence si des signatures sont pendantes, puis utilise uniquement `getSignatureStatuses` et `getTransaction`. Il ne resoumet pas les transactions, ne traite pas une signature absente comme un échec, ne reconstruit pas une position depuis une cotation et ne lève pas automatiquement l'arrêt.
- Les échecs finalisés avec frais récupérés sont archivés et leur coût en lamports est signalé séparément du PnL USD. Le rapprochement PnL historique reste nécessaire.
- Une différence entre débit token confirmé et montant exact-in demandé est traitée comme non comptabilisée et reste bloquée pour rapprochement; aucune tolérance de frais de transfert n'est appliquée.
- Les métriques p50/p95/p99 excluent mints, wallets, signatures, soldes, clés et corps de réponse. Les noms d'étape et voies RPC sont en liste blanche. L'écriture disque passe par une file bornée best-effort, et Telegram la lit hors de la boucle asyncio avec délai maximal.

Le détail des contrôles, du risque de redirection corrigé et des limites CPI/DEX est dans [docs/JUPITER_SECURITY_REVIEW.md](docs/JUPITER_SECURITY_REVIEW.md). Les tests de redirection sont simulés et ne remplacent pas une simulation Jupiter sur RPC ni une revue indépendante des programmes on-chain.

## Secrets et incident

Ne versionnez ni clés privées ni clés API. Si un secret est exposé, révoquez-le, remplacez-le et inspectez l'historique Git. En cas de dépense ou signature inattendue, gardez `TRADING_MODE=PAPER`, utilisez `/halt` si le bot est accessible, conservez les signatures et réconciliez l'état avant toute reprise.
