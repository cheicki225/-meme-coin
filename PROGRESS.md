# Progression vérifiable

Mise à jour : 22 novembre 2026. La progression mesure le périmètre de développement, pas la rentabilité, la disponibilité en production ni l'autorisation d'utiliser des fonds.

## Avancement pondéré

Chaque objectif compte de façon égale. Barème publié pour rendre l'estimation reproductible : `VERIFIED = 100`, `TESTED = 75`, `IMPLEMENTED = 50`, `IN PROGRESS = 25`, `NOT STARTED/BLOCKED = 0`. Un test local mocké ne vaut pas validation d'exploitation.

| # | Objectif | État | Score | Preuve et limite actuelle |
|---:|---|---|---:|---|
| 1 | Revue des 16 fichiers de la PR #3 | VERIFIED | 100 | Liste et constats consignés dans [AUDIT_COMPLET.md](AUDIT_COMPLET.md) |
| 2 | Sécurité Jupiter et transactions | TESTED | 75 | Quote liée aux arguments exact-in et au minimum reçu appliqué; hard caps 10 % slippage/5 % impact; frais, payeurs, ALT/ATA, redirections HTTP et débit token confirmé exact testés. CPI/DEX non audités indépendamment; tokens à frais de transfert non pris en charge |
| 3 | Collecteur et bot | TESTED | 75 | Collecteur lecture seule, alertes partielles, reprise du curseur après échec SQLite, historique limité et commande Telegram `/activity` testés; événements séparés des ordres; pas de soak fournisseur |
| 4 | PnL réel et frais | TESTED | 75 | LIVE utilise deltas/frais confirmés; PAPER comptabilise frais d'entrée, DCA, sorties et Sell Initials. Les anciens PnL non versionnés sont signalés; cours USD non historisé |
| 5 | SQLite et reprise | TESTED | 75 | WAL, sauvegardes, import strict, rollback des mutations et protection contre registres mal formés testés; succès sans position reste bloqué; aucune restauration en déploiement |
| 6 | RPC et erreurs | TESTED | 75 | Retry 429/5xx, `Retry-After`, jitter, compteurs et métriques p50/p95/p99 par étape et voie primaire/repli testés; aucune mesure sur RPC déployé, charge ou fraîcheur observée |
| 7 | Monitoring et Telegram | TESTED | 75 | Retry d'alerte, `/performance` hors boucle asyncio, `/reconcile`, arrêt avant inspection, statut du coupe-circuit et frais d'échecs en lamports testés; aucun envoi Telegram de déploiement vérifié |
| 8 | Backtesting | IN PROGRESS | 25 | Dataset OHLC versionné/hashé, timestamps signaux exacts, coûts, gaps et drawdown mark-to-market intratrade conservateur; authenticité des sources et intégration complète à la stratégie restent absentes |
| 9 | Tests automatisés | TESTED | 75 | 357 tests locaux et CI réussis sur le commit `a30f11a`: compilation, défaut PAPER, pytest, Ruff, pip-audit sans vulnérabilités connues et Gitleaks sans secret détecté |
| 10 | Pertes, exposition, arrêt d'urgence | TESTED | 75 | Garde d'exposition, réservations, maintien du stop en mémoire en cas d'échec de sauvegarde et refus d'état mal formé testés; en LIVE l'arrêt bloque achats et ventes sans liquider; aucun soak PAPER |

**Avancement pondéré : 72,5 %** (`725 / 10`). Cette estimation reste inchangée, car les ajouts de cette mission renforcent des objectifs déjà notés mais ne ferment pas leurs validations d'exploitation. Elle mesure une maturité d'implémentation selon le barème ci-dessus, pas un taux de fonctionnalités prêtes pour des fonds.

## État de préparation par contexte

- **Développement : 72,5 %**, score pondéré documenté ci-dessus.
- **Tests locaux :** compilation réussie et **357 tests réussis** avec `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`; le taux de couverture lignes/branches n'a pas été mesuré.
- **Sécurité : IN PROGRESS.** CI [45059131294](https://github.com/cheicki225/-meme-coin/actions/runs/45059131294) verte sur `a30f11a`; l'audit indépendant des programmes CPI/DEX reste ouvert.
- **PAPER : IN PROGRESS.** C'est le mode par défaut; frais simulés intégrés et testés. Il reste à faire un soak et une restauration dans l'environnement cible.
- **LIVE : BLOCKED / NOT READY.** Aucune transaction réelle exécutée; pas d'intégration mainnet, soak, restauration en production ou revue indépendante complète. Ne pas activer les deux opt-ins LIVE.

## Hypothèses non vérifiées

Le code permet l'HTTP Solana public par défaut, mais le point d'accès réellement utilisé au déploiement est **UNKNOWN**. Aucun fichier `.env`, secret, tableau de bord fournisseur ni journal privé n'a été lu. Sans clé Helius, le mode PAPER démarre en capacité dégradée : les listeners WebSocket et les transactions Helius Enhanced ne sont pas disponibles.

Voir [PROJECT_ROADMAP.md](PROJECT_ROADMAP.md), [TESTING.md](TESTING.md) et [LIVE_READINESS.md](LIVE_READINESS.md).

## Mission performance du 22 novembre 2026

- Branche locale : `codex/mission-performance-2026-11-22`, basée sur le head vérifié de la PR #4.
- Ajouts testés localement : prévention des redirections Jupiter, validation exacte du débit token confirmé, métriques filtrées par liste blanche et voie RPC, commandes Telegram de performance/réconciliation/activité en lecture seule, reprise fail-closed des registres mal formés, alertes partielles du collecteur, rollback du curseur sur échec SQLite, gestion d'erreurs réseau après signature, rollback des mutations d'état et drawdown mark-to-market intratrade.
- Compilation locale : réussie; suite complète : **357 réussites**; `git diff --check` : réussi. La CI complète [45059131294](https://github.com/cheicki225/-meme-coin/actions/runs/45059131294) est verte sur `a30f11a`, y compris Ruff, pip-audit et Gitleaks.
- Aucun soak PAPER dans l'environnement cible, appel RPC réel de réconciliation, transaction réelle ou audit CPI/DEX indépendant n'ont été exécutés. Ruff/pip-audit/Gitleaks ne sont pas installés localement; leur installation isolée a échoué sur la vérification TLS de PyPI, mais les trois étapes sont vertes dans la CI de la branche. Les actions GitHub ont été migrées vers Node 24 après un avertissement, puis la CI complète a réussi sans annotation.
- Les PR #1 à #4 ont été vérifiées sur GitHub; elles restent ouvertes et aucune fusion n'a été effectuée. Voir [docs/PR_STACK_INTEGRATION_REVIEW.md](docs/PR_STACK_INTEGRATION_REVIEW.md).
