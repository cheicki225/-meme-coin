# Progression vérifiable

Mise à jour : 22 novembre 2026. La progression mesure le périmètre de développement, pas la rentabilité, la disponibilité en production ni l'autorisation d'utiliser des fonds.

## Avancement pondéré

Chaque objectif compte de façon égale. Barème publié pour rendre l'estimation reproductible : `VERIFIED = 100`, `TESTED = 75`, `IMPLEMENTED = 50`, `IN PROGRESS = 25`, `NOT STARTED/BLOCKED = 0`. Un test local mocké ne vaut pas validation d'exploitation.

| # | Objectif | État | Score | Preuve et limite actuelle |
|---:|---|---|---:|---|
| 1 | Revue des 16 fichiers de la PR #3 | VERIFIED | 100 | Liste et constats consignés dans [AUDIT_COMPLET.md](AUDIT_COMPLET.md) |
| 2 | Sécurité Jupiter et transactions | TESTED | 75 | Quote liée aux arguments exact-in et au minimum reçu appliqué; hard caps 10 % slippage/5 % impact; frais, payeurs, ALT/ATA, redirections HTTP et débit token confirmé exact testés. Mission 6 ajoute une simulation pré-signature, traces CPI et allowlist vide par défaut; revue sémantique/programme DEX non fermée; débit token confirmé toujours après swap; tokens à frais non pris en charge |
| 3 | Collecteur et bot | TESTED | 75 | Collecteur lecture seule, alertes partielles, reprise du curseur après échec SQLite, historique limité et commande Telegram `/activity` testés; événements séparés des ordres; pas de soak fournisseur |
| 4 | PnL réel et frais | TESTED | 75 | LIVE utilise deltas/frais confirmés; PAPER comptabilise frais d'entrée, DCA, sorties et Sell Initials. Les anciens PnL non versionnés sont signalés; cours USD non historisé |
| 5 | SQLite et reprise | TESTED | 75 | WAL, sauvegardes, import strict, rollback, refus du downgrade SQLite engagé vers JSON obsolète, commit survivant à `os._exit` et échec d'écriture contrôlé testés; aucune restauration en déploiement |
| 6 | RPC et erreurs | TESTED | 75 | Retry 429/5xx, `Retry-After`, jitter, compteurs par tentative, fallback et récupération SQLite mesurés sur fixtures locales; aucune mesure sur RPC déployé, charge/fraîcheur réelle |
| 7 | Monitoring et Telegram | TESTED | 75 | Retry d'alerte, `/performance` hors boucle asyncio, `/reconcile`, arrêt avant inspection, statut du coupe-circuit et frais d'échecs en lamports testés; aucun envoi Telegram de déploiement vérifié |
| 8 | Backtesting | IN PROGRESS | 25 | Dataset OHLC versionné/hashé, timestamps signaux exacts, coûts, gaps et drawdown mark-to-market intratrade conservateur; authenticité des sources, frais historiques complets et intégration stratégie restent absents |
| 9 | Tests automatisés | TESTED | 75 | **378 tests locaux réussis** sur la branche Mission 6, incluant arrêt enfant `os._exit`, CPI simulés et échec SQLite. CI configurée pour publier couverture ligne/branche; pourcentage CI final en attente |
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

## Mission 6 — validations indépendantes et benchmark — 22 novembre 2026

- Branche dédiée : `codex/mission-6-validation-2026-11-22`, issue du head vérifié de la PR #5, `7fe6db45b1d661738a38006bbd8278bfc62b7e06`. `main`, son worktree original et les PR #1 à #5 sont préservés; aucune fusion n'a été faite.
- Six sous-agents spécialisés ont vérifié Jupiter/CPI, récupération, RPC/performance, comptabilité/backtest, tests/qualité et sécurité indépendante. Résumé détaillé : [docs/MISSION_6_VALIDATION.md](docs/MISSION_6_VALIDATION.md).
- Correctifs : blocage fail-closed si une base SQLite engagée ne peut pas être restaurée depuis SQLite; validation positive/finie de `CIRCUIT_BREAKER_USD`; simulation CPI pré-signature avec allowlist vide par défaut; métriques RPC par tentative et acceptation de `solana_ws`.
- Résultats locaux : **378 tests réussis**, compilation et `git diff --check` réussis. Couverture locale absente; couverture ligne/branche désormais configurée en CI, valeur finale non encore confirmée.
- Soak/benchmark : 10 000 cycles PAPER synthétiques vers localhost, 24h virtuelles en 73,507 s, 19 réouvertures SQLite; RPC loopback p50/p95/p99 = 6,699/10,357/19,921 ms; ce n'est pas une mesure mainnet ni un soak de 24h réel. Voir [docs/MISSION_6_BENCHMARK.md](docs/MISSION_6_BENCHMARK.md) et [JSON brut](docs/benchmarks/mission6-paper-soak.json).
- Provenance OHLC, coûts historiques complets, audit sémantique des programmes CPI/DEX, soak sur endpoint PAPER cible et restauration de déploiement demeurent non vérifiés. LIVE reste bloqué.
