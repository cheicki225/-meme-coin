# Progression vérifiable

Mise à jour : 15 novembre 2026. La progression mesure le périmètre de développement, pas la rentabilité, la disponibilité en production ni l'autorisation d'utiliser des fonds.

## Avancement pondéré

Chaque objectif compte de façon égale. Barème publié pour rendre l'estimation reproductible : `VERIFIED = 100`, `TESTED = 75`, `IMPLEMENTED = 50`, `IN PROGRESS = 25`, `NOT STARTED/BLOCKED = 0`. Un test local mocké ne vaut pas validation d'exploitation.

| # | Objectif | État | Score | Preuve et limite actuelle |
|---:|---|---|---:|---|
| 1 | Revue des 16 fichiers de la PR #3 | VERIFIED | 100 | Liste et constats consignés dans [AUDIT_COMPLET.md](AUDIT_COMPLET.md) |
| 2 | Sécurité Jupiter et transactions | TESTED | 75 | Ordres, montants, payeurs, transactions v0, ALT, ATA, frais et résultats confirmés testés localement; routes CPI/DEX non auditées indépendamment |
| 3 | Collecteur et bot | TESTED | 75 | Collecteur lecture seule, curseur/idempotence, panne RPC distincte d'un historique vide et mode sans clé Helius testés; pas de soak fournisseur |
| 4 | PnL réel et frais | TESTED | 75 | Deltas on-chain + frais vérifiés; flux SOL de vente réutilise le résultat confirmé. Frais PAPER et historique USD encore incomplets |
| 5 | SQLite et reprise | TESTED | 75 | WAL, sauvegardes, import, restauration et signatures pendantes couverts par tests; restauration non exercée en déploiement |
| 6 | RPC et erreurs | TESTED | 75 | Retry 429/5xx, `Retry-After`, jitter et journaux sans exception URL testés; charge, fraîcheur et endpoint déployé inconnus |
| 7 | Monitoring et Telegram | IMPLEMENTED | 50 | Métriques, alertes, `/halt` et `/resume` existent; aucun envoi Telegram de déploiement vérifié |
| 8 | Backtesting | IN PROGRESS | 25 | Replay OHLC et chemin GeckoTerminal chronologique existent; moteur OHLC pas relié au pipeline d'évaluation complet, historique réel/provenance absents |
| 9 | Tests automatisés | TESTED | 75 | Suite locale passée; CI complète de la nouvelle révision à confirmer sur GitHub |
| 10 | Pertes, exposition, arrêt d'urgence | TESTED | 75 | Garde d'exposition, réservations, pertes, signatures pendantes et actions PAPER héritées testées; aucun soak PAPER |

**Avancement pondéré : 70 %** (`700 / 10`). La valeur est une estimation de maturité d'implémentation selon le barème ci-dessus; ce n'est pas un taux de fonctionnalités prêtes pour des fonds.

## État de préparation par contexte

- **Développement : 70 %**, score pondéré documenté ci-dessus.
- **Tests locaux :** `compileall` réussi et **170 tests passés** avec `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`; le taux de couverture lignes/branches n'a pas été mesuré.
- **Sécurité : IN PROGRESS.** Correctifs et tests locaux présents; CI dépendance/secrets de cette révision à confirmer. Aucun rapport d'audit indépendant des programmes CPI/DEX.
- **PAPER : IN PROGRESS.** C'est le mode par défaut. Il reste à faire un soak et une restauration dans l'environnement cible; ses frais simulés ne sont pas intégrés au PnL courant.
- **LIVE : BLOCKED / NOT READY.** Aucune transaction réelle exécutée; pas d'intégration mainnet, soak, restauration en production ou revue indépendante complète. Ne pas activer les deux opt-ins LIVE.

## Hypothèses non vérifiées

Le code permet l'HTTP Solana public par défaut, mais le point d'accès réellement utilisé au déploiement est **UNKNOWN**. Aucun fichier `.env`, secret, tableau de bord fournisseur ni journal privé n'a été lu. Sans clé Helius, le mode PAPER démarre en capacité dégradée : les listeners WebSocket et les transactions Helius Enhanced ne sont pas disponibles.

Voir [PROJECT_ROADMAP.md](PROJECT_ROADMAP.md), [TESTING.md](TESTING.md) et [LIVE_READINESS.md](LIVE_READINESS.md).
