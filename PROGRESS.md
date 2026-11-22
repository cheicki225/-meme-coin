# Progression vérifiable

Mise à jour : 22 novembre 2026. La progression mesure le périmètre de développement, pas la rentabilité, la disponibilité en production ni l'autorisation d'utiliser des fonds.

## Avancement pondéré

Chaque objectif compte de façon égale. Barème publié pour rendre l'estimation reproductible : `VERIFIED = 100`, `TESTED = 75`, `IMPLEMENTED = 50`, `IN PROGRESS = 25`, `NOT STARTED/BLOCKED = 0`. Un test local mocké ne vaut pas validation d'exploitation.

| # | Objectif | État | Score | Preuve et limite actuelle |
|---:|---|---|---:|---|
| 1 | Revue des 16 fichiers de la PR #3 | VERIFIED | 100 | Liste et constats consignés dans [AUDIT_COMPLET.md](AUDIT_COMPLET.md) |
| 2 | Sécurité Jupiter et transactions | TESTED | 75 | Quote liée aux arguments exact-in et au minimum reçu appliqué; hard caps 10 % slippage/5 % impact; frais, payeurs, ALT/ATA et redirections HTTP refusées testés. CPI/DEX non audités indépendamment |
| 3 | Collecteur et bot | TESTED | 75 | Collecteur optionnel lecture seule, curseur/idempotence et retry de signature RPC testés; événements conservés en historique et non utilisés directement comme ordres; pas de soak fournisseur |
| 4 | PnL réel et frais | TESTED | 75 | LIVE utilise deltas/frais confirmés; PAPER comptabilise frais d'entrée, DCA, sorties et Sell Initials. Les anciens PnL non versionnés sont signalés; cours USD non historisé |
| 5 | SQLite et reprise | TESTED | 75 | WAL, sauvegardes, import strict, rollback des mutations et protection contre registres mal formés testés; succès sans position reste bloqué; aucune restauration en déploiement |
| 6 | RPC et erreurs | TESTED | 75 | Retry 429/5xx, `Retry-After`, jitter, compteurs et métriques p50/p95/p99 par étape et voie primaire/repli testés; aucune mesure sur RPC déployé, charge ou fraîcheur observée |
| 7 | Monitoring et Telegram | TESTED | 75 | Retry d'alerte, `/performance` hors boucle asyncio, `/reconcile`, arrêt avant inspection, statut du coupe-circuit et frais d'échecs en lamports testés; aucun envoi Telegram de déploiement vérifié |
| 8 | Backtesting | IN PROGRESS | 25 | Dataset OHLC versionné/hashé, timestamps signaux exacts, coûts et gaps annotés; authenticité des sources et intégration complète à la stratégie restent absentes |
| 9 | Tests automatisés | TESTED | 75 | 348 tests locaux réussis sur la branche dédiée; la CI [45057315402](https://github.com/cheicki225/-meme-coin/actions/runs/45057315402) est verte sur `4456952`, avec compilation, défaut PAPER, pytest, Ruff, pip-audit et Gitleaks |
| 10 | Pertes, exposition, arrêt d'urgence | TESTED | 75 | Garde d'exposition, réservations, maintien du stop en mémoire en cas d'échec de sauvegarde et refus d'état mal formé testés; en LIVE l'arrêt bloque achats et ventes sans liquider; aucun soak PAPER |

**Avancement pondéré : 72,5 %** (`725 / 10`). Cette estimation reste inchangée, car les ajouts de cette mission renforcent des objectifs déjà notés mais ne ferment pas leurs validations d'exploitation. Elle mesure une maturité d'implémentation selon le barème ci-dessus, pas un taux de fonctionnalités prêtes pour des fonds.

## État de préparation par contexte

- **Développement : 72,5 %**, score pondéré documenté ci-dessus.
- **Tests locaux :** compilation réussie et **348 tests réussis** avec `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`; le taux de couverture lignes/branches n'a pas été mesuré.
- **Sécurité : IN PROGRESS.** CI [45057315402](https://github.com/cheicki225/-meme-coin/actions/runs/45057315402) verte sur `4456952`; l'audit indépendant des programmes CPI/DEX reste ouvert.
- **PAPER : IN PROGRESS.** C'est le mode par défaut; frais simulés intégrés et testés. Il reste à faire un soak et une restauration dans l'environnement cible.
- **LIVE : BLOCKED / NOT READY.** Aucune transaction réelle exécutée; pas d'intégration mainnet, soak, restauration en production ou revue indépendante complète. Ne pas activer les deux opt-ins LIVE.

## Hypothèses non vérifiées

Le code permet l'HTTP Solana public par défaut, mais le point d'accès réellement utilisé au déploiement est **UNKNOWN**. Aucun fichier `.env`, secret, tableau de bord fournisseur ni journal privé n'a été lu. Sans clé Helius, le mode PAPER démarre en capacité dégradée : les listeners WebSocket et les transactions Helius Enhanced ne sont pas disponibles.

Voir [PROJECT_ROADMAP.md](PROJECT_ROADMAP.md), [TESTING.md](TESTING.md) et [LIVE_READINESS.md](LIVE_READINESS.md).

## Mission performance du 22 novembre 2026

- Branche locale : `codex/mission-performance-2026-11-22`, basée sur le head vérifié de la PR #4.
- Ajouts testés localement : prévention des redirections Jupiter, métriques filtrées par liste blanche et voie RPC, commandes Telegram de performance et réconciliation en lecture seule, reprise fail-closed des registres mal formés, gestion d'erreurs réseau après signature et rollback des mutations d'état.
- Compilation locale : réussie; suite complète : **348 réussites**; `git diff --check` : réussi.
- Aucun soak PAPER dans l'environnement cible, appel RPC réel de réconciliation, transaction réelle ou audit CPI/DEX indépendant n'ont été exécutés. Ruff/pip-audit/Gitleaks ne sont pas installés localement; leur installation isolée a échoué sur la vérification TLS de PyPI, mais les trois étapes sont vertes dans la CI de la branche. Les actions GitHub ont été migrées vers Node 24 après un avertissement, puis la CI complète a réussi sans annotation.
- Les PR #1 à #4 ont été vérifiées sur GitHub; elles restent ouvertes et aucune fusion n'a été effectuée. Voir [docs/PR_STACK_INTEGRATION_REVIEW.md](docs/PR_STACK_INTEGRATION_REVIEW.md).
