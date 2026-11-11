# Sécurité et statut de l'exécution

## Mode de fonctionnement

Gardez `TRADING_MODE=PAPER` et `LIVE_EXECUTION_ENABLED=false`. Le code LIVE exige ces deux opt-ins simultanément, mais il n'a pas été validé avec un RPC de production, un portefeuille de test, ni un soak test. Aucun swap réel n'a été exécuté au cours de cette mise à jour.

## Garde-fous implémentés

- Jupiter Swap API V2 utilise les endpoints officiels fixes `/swap/v2/order` et `/swap/v2/execute`, avec `JUPITER_API_KEY`. Les routeurs JupiterZ, Dflow et OKX sont exclus; seule une route Metis est acceptée.
- Avant signature, l'exécuteur résout les Address Lookup Tables depuis le RPC, puis vérifie les mints et montants, le minimum reçu, le slippage, l'impact, le wallet taker/payeur, le nombre de signataires, les programmes top-level, les ATA du wallet, le blockhash, les frais réseau et le loyer de comptes estimé. Une donnée manquante ou un programme inconnu fait échouer la transaction.
- La signature est créée localement. L'API reçoit la transaction signée pour l'inclusion; la clé privée n'est jamais envoyée.
- La signature est persistée avant l'appel d'exécution. Les issues incertaines ne sont jamais renvoyées automatiquement et activent l'arrêt d'urgence.
- Une vente/position n'est comptabilisée qu'après confirmation `finalized` et lecture du `getTransaction`. Le PnL SOL inclut les frais réseau et le loyer réellement reflétés dans les soldes du wallet. La conversion USD utilise le cours SOL récupéré au moment du trade; elle n'est pas une valorisation historique de marché.
- Les décimales de tokens sont prises dans les métadonnées confirmées. Les positions LIVE MultiBuy sont refusées jusqu'à une comptabilisation par wallet.
- Les transferts SOL manuels sont bloqués en PAPER, limités par montant en LIVE et refusés pendant l'arrêt d'urgence ou une transaction pendante. Les bundles Jito avec transfert de tip séparé restent désactivés.
- L'état du bot est stocké en SQLite WAL avec commits atomiques, import initial depuis l'ancien JSON, sauvegardes et restauration depuis un instantané valide. Une version de schéma plus récente n'est jamais rétrogradée.
- `/halt` déclenche un arrêt d'urgence durable; `/resume` ne le retire pas tant qu'une signature reste pendante. La limite de positions et d'exposition globale est vérifiée avant un nouvel achat et protégée contre les entrées concurrentes.
- Le collecteur de wallets et le moniteur RPC sont en lecture seule; leurs erreurs génèrent des alertes Telegram avec cooldown.

## Limites avant toute activation LIVE

1. Les instructions CPI d'un routeur et les programmes DEX appelés ne sont pas audités programme par programme. La liste des instructions top-level est une politique fail-closed, pas une preuve formelle d'innocuité on-chain.
2. Les signatures pendantes non attachées à une position mettent le bot en arrêt d'urgence. Leur réconciliation après redémarrage reste manuelle; aucun retry ni reconstruction approximative de position n'est effectué.
3. L'intégration Jupiter V2 n'a pas été exécutée sur devnet/mainnet. Les tests sont mockés et ne prouvent ni la disponibilité des routes ni le comportement du RPC cible.
4. Le calcul USD utilise un cours SOL instantané au moment de l'exécution; les historiques de prix complets, les frais de fournisseur et les écarts de valeur SOL/USD entre entrée et sortie ne sont pas reconstruits.
5. Le moteur de backtest OHLC requiert des données de chandelles fournies par l'opérateur. Aucun fournisseur historique n'est encore branché et le scoring historique de wallets du bot demeure heuristique.
6. SQLite garantit l'atomicité du document d'état pour un processus du bot; aucun verrouillage multi-processus ni sauvegarde hors machine n'est fourni.
7. Les fonctionnalités existantes de copy trading, de Buy on Dev Sell et de détection de régularité conservent les limites documentées dans README.

## Validation de sécurité requise

Avant toute décision LIVE, terminer la CI de la branche de suivi, tester les réponses Jupiter adversariales, exécuter un soak PAPER dans l'environnement cible, vérifier la restauration et la reprise, puis faire une revue indépendante des instructions on-chain et du PnL. N'utilisez pas un wallet qui contient des fonds à long terme.

## Secrets et incident

Ne versionnez ni clés privées ni clés API. Si un secret est exposé, révoquez-le, remplacez-le et inspectez l'historique Git. En cas de dépense ou signature inattendue, gardez `TRADING_MODE=PAPER`, utilisez `/halt` si le bot est accessible, conservez les signatures et réconciliez l'état avant toute reprise.
