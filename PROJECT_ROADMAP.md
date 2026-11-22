# Feuille de route du bot Solana

Mise à jour : 22 novembre 2026. Cette feuille décrit l'état du code audité dans la branche de suivi de la PR #4. Les fonctionnalités distantes et l'environnement de déploiement ne sont pas présumés disponibles.

## Priorités

| Priorité | Travail | État | Critère de sortie |
|---|---|---|---|
| P0 | Garder PAPER par défaut; verrouiller les changements d'état LIVE non confirmés | TESTED | Tests sans émission réseau réelle; aucun état de position modifié par une simulation héritée en LIVE |
| P0 | Sécuriser Jupiter, transactions signées, frais, erreurs et reprises | TESTED | Minimum Jupiter exactement lié à la route; caps 10 % slippage/5 % impact; revue CPI/DEX indépendante et simulation du fournisseur réel encore nécessaires |
| P0 | Réconcilier les signatures ambiguës au redémarrage et maintenir le kill switch | TESTED | Continuer à refuser les nouvelles exécutions tant que l'issue d'une signature est inconnue |
| P1 | Faire fonctionner l'intelligence de wallets sur RPC HTTP public avec états explicites | TESTED | Collecte optionnelle lecture seule, idempotente et réessayable testée; événements pas reliés aux ordres; mesurer le fournisseur déployé |
| P1 | Rendre le PnL transparent sur les flux confirmés, frais et cours de conversion | IN PROGRESS | PnL LIVE depuis deltas on-chain; frais PAPER intégrés; frais historiques non reconstructibles signalés; snapshots USD historiques restent à finir |
| P1 | Améliorer la tolérance aux pannes RPC et les alertes | TESTED | Retry-after/backoff/jitter et reprise d'alerte après échec de livraison testés; ajouter fraîcheur de slot, mesure de charge et test de longue durée |
| P1 | Relier le replay historique à des données et à la stratégie réellement utilisée | IN PROGRESS | Primitive OHLC déterministe, format dataset versionné/hashé et gaps annotés; source authentifiée, intégration stratégie et lookahead bout en bout restent absents |
| P2 | Valider restauration, redémarrage et alertes dans l'environnement cible | NOT STARTED | Exercice PAPER documenté avec sauvegarde et preuves d'alertes |
| P2 | Terminer CI et audit des outils sur le commit de cette mise à jour | IN PROGRESS | Run #199 vert sur le commit précédent `aa5a001`; attendre compilation, tests, Ruff, pip-audit et Gitleaks sur le nouveau commit |

## Dépendances

1. Aucun test d'intégration ne doit signer ni transmettre une transaction réelle.
2. Toute décision LIVE reste suspendue jusqu'à la fin de la revue de sécurité et de l'exercice PAPER.
3. Le fournisseur RPC déployé, ses quotas et ses historiques doivent être confirmés par l'opérateur; le dépôt n'inspecte ni `.env` ni tableau de bord.
4. Le système de backtest doit conserver provenance, fuseau/horodatage, trous de données, coûts et règles d'entrée/sortie avant de contribuer à une décision automatique.

Voir [PROGRESS.md](PROGRESS.md), [LIVE_READINESS.md](LIVE_READINESS.md), [RPC_PUBLIC_LIMITATIONS.md](RPC_PUBLIC_LIMITATIONS.md) et [BACKTESTING.md](BACKTESTING.md).
