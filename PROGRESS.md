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
| 9 | Tests automatisés | TESTED | 75 | **378 tests réussis localement et en CI**; arrêt enfant `os._exit`, CPI simulés et échec SQLite. Couverture CI vérifiée : 31,89 % lignes (3 226/10 116), 24,71 % branches (922/3 732), 29,95 % combiné; ce taux global n'atteste pas la validation des parcours financiers manquants |
| 10 | Pertes, exposition, arrêt d'urgence | TESTED | 75 | Garde d'exposition, réservations, maintien du stop en mémoire en cas d'échec de sauvegarde et refus d'état mal formé testés; en LIVE l'arrêt bloque achats et ventes sans liquider; aucun soak PAPER |

**Avancement pondéré : 72,5 %** (`725 / 10`). Cette estimation reste inchangée, car les ajouts de cette mission renforcent des objectifs déjà notés mais ne ferment pas leurs validations d'exploitation. Elle mesure une maturité d'implémentation selon le barème ci-dessus, pas un taux de fonctionnalités prêtes pour des fonds.

## État de préparation par contexte

- **Développement : 72,5 %**, score pondéré documenté ci-dessus.
- **Tests locaux :** compilation réussie et **378 tests réussis** avec `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`; couverture CI vérifiée ci-dessous.
- **Sécurité : IN PROGRESS.** CI Mission 6 [45065197903](https://github.com/cheicki225/-meme-coin/actions/runs/45065197903) verte; l'audit sémantique indépendant des programmes CPI/DEX reste ouvert.
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

- Branche dédiée : `codex/mission-6-validation-2026-11-22`, issue du head vérifié de la PR #5, `7fe6db45b1d661738a38006bbd8278bfc62b7e06`. Implémentation Mission 6 : `817ddc4b55494abe3be674ba226528ccf3fb80d5`. `main`, son worktree original et les PR #1 à #5 sont préservés; aucune fusion n'a été faite.
- Six sous-agents spécialisés ont vérifié Jupiter/CPI, récupération, RPC/performance, comptabilité/backtest, tests/qualité et sécurité indépendante. Résumé détaillé : [docs/MISSION_6_VALIDATION.md](docs/MISSION_6_VALIDATION.md).
- Correctifs : blocage fail-closed si une base SQLite engagée ne peut pas être restaurée depuis SQLite; validation positive/finie de `CIRCUIT_BREAKER_USD`; simulation CPI pré-signature avec allowlist vide par défaut; métriques RPC par tentative et acceptation de `solana_ws`.
- Résultats locaux et CI : **378 tests réussis**, compilation et `git diff --check` réussis. Couverture CI vérifiée : **31,89 % lignes**, **24,71 % branches**, **29,95 % combiné**; détails et lien dans [docs/MISSION_6_COVERAGE.md](docs/MISSION_6_COVERAGE.md).
- Soak/benchmark : 10 000 cycles PAPER synthétiques vers localhost, 24h virtuelles en 71,928 s, 19 réouvertures SQLite; RPC loopback p50/p95/p99 = 6,473/10,236/18,825 ms; ce n'est pas une mesure mainnet ni un soak de 24h réel. Voir [docs/MISSION_6_BENCHMARK.md](docs/MISSION_6_BENCHMARK.md) et [JSON brut](docs/benchmarks/mission6-paper-soak.json).
- Provenance OHLC, coûts historiques complets, audit sémantique des programmes CPI/DEX, soak sur endpoint PAPER cible et restauration de déploiement demeurent non vérifiés. LIVE reste bloqué.

## Mission 7 — état vérifié avant Mission 8 — 22 novembre 2026

- La PR #8 (`codex/mission-7-reliability-2026-11-22`) est ouverte, non fusionnée, empilée sur la PR #6 et pointe sur `6f082a2c60ed5a017ff5420fc663f1b919205ecc`. Son run CI `45073157671` est terminé avec succès. Les 424 tests indiqués dans sa description et cette CI vérifient des chemins offline; ils ne ferment pas les gates d'exploitation.
- Mission 7 reste `INCOMPLETE / MANUAL_VERIFICATION_REQUIRED` : le soak PAPER cible de 24 h, un exercice de restauration dans l'environnement cible, la validation du fournisseur/quota effectif et l'audit de la sémantique/hash des programmes DEX ne sont pas établis. LIVE reste bloqué.
- Le dépôt courant n'inclut pas `web-research-problem-solver`. Le catalogue de la PR #7, non fusionnée et distincte de cette pile, précise que ce skill externe conditionnel n'est pas livré. État : `SKILL_UNAVAILABLE`; aucun blocage de code restant n'a nécessité son handoff.

## Mission 8 — optimisation RPC/WebSocket — 22 novembre 2026

- Branche dédiée `codex/mission-8-performance-2026-11-22`, empilée sur le head de la PR #8; la PR M8 cible cette branche tant que M7 reste ouverte. Changements limités aux quotas et au cooldown RPC, au regroupement de lectures simultanées, à la politique de retries des lectures, aux métriques et à la reconnexion WebSocket. Aucun réglage de risque ni opt-in LIVE n'est modifié.
- Les mesures en lecture seule de la machine Codex sont décrites dans [le rapport M8](docs/MISSION_8_PERFORMANCE.md) et son [résumé JSON](docs/benchmarks/mission8-public-rpc-readonly.json). Elles ne représentent pas le fournisseur ou la région du déploiement et ne constituent pas un test de charge.
- Validation locale M8 : compilation réussie, **440 tests réussis**, couverture combinée affichée 31,44 %, sélection Ruff CI réussie. `pip-audit` local a échoué pendant la préparation de son environnement temporaire et Gitleaks n'est pas installé localement; aucun résultat propre de ces deux contrôles n'est revendiqué ici. La CI M8 doit être vérifiée sur la PR M8, sans reprendre le résultat de M7.
- La progression pondérée reste inchangée : les modifications de performance n'ont fermé aucune validation d'exploitation de M7.
