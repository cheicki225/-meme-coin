# Mission 6 — rapport de validation

Date : 22 novembre 2026. Point de départ : tête PR #5, commit `7fe6db45b1d661738a38006bbd8278bfc62b7e06`.

## Résumé

Le travail est isolé sur `codex/mission-6-validation-2026-11-22`, issue du head vérifié de la PR #5. `main`, le worktree original et les PR #1 à #5 sont préservés et non fusionnés. PAPER est resté le mode effectif; aucun portefeuille, RPC public, endpoint Jupiter ou programme DEX n'a été utilisé. Les clés sont vides dans le harnais, dotenv y est désactivé, et aucun secret n'a été affiché ou commité. Aucune transaction n'a été signée ou soumise.

La suite complète a donné **378 tests réussis, 0 échec**. Compilation et `git diff --check` ont réussi. Le pourcentage de couverture n'est pas disponible localement; la CI calcule maintenant les couvertures ligne/branche et téléverse les résultats. Le chiffre ne sera consigné qu'après exécution vérifiée de la CI finale.

Le soak synthétique/accéléré équivalent à 24h virtuelles a exécuté **10 000 cycles** avec PAPER, dispatch synthétique, primitives buy/sell, SQLite temporaire, retries HTTP 429/503, fallback et 19 réouvertures SQLite. Durée murale : 71,928 s sur le code `817ddc4`. Ce n'est pas un test de 24 heures réelles ou du bot complet.

## Graphe des branches et des PR

Le graphe GitHub contrôlé avant modification montrait les PR ouvertes et non fusionnées :

```text
main
├── PR #1 — improvements/safety-observability
│   └── PR #2 — nextgen/performance-observability (base sur la branche #1)
├── PR #3 — codex/audit-securite-fiabilite
│   └── PR #4 — codex/bot-reliability-phase-2 (base sur la branche #3)
│       └── PR #5 — codex/mission-performance-2026-11-22 (base sur la branche #4)
│           └── codex/mission-6-validation-2026-11-22 (ce travail)
```

La nouvelle branche part du commit exact de la PR #5; aucune modification ni fusion n'a été faite aux PR antérieures. La publication d'une nouvelle PR est séparée de toute fusion.

La PR [#6](https://github.com/cheicki225/-meme-coin/pull/6) est ouverte et cible la branche de la PR #5. Lors de sa création, GitHub n'avait encore associé aucun run CI à sa tête; les résultats de la CI ne sont donc pas supposés ici.

## Orchestration parallèle

Six sous-agents spécialisés ont été lancés en parallèle. Leurs audits initiaux étaient en lecture seule; l'intégration et les corrections ont été faites après réception des constats.

| Spécialité | Résultat utile |
|---|---|
| Jupiter | Les contrôles de premier niveau ne couvraient pas les CPI. Ajout d'une simulation RPC et d'une allowlist fail-closed; audit sémantique DEX non clos. |
| Récupération/persistance | Reproduction sur copie temporaire du downgrade SQLite corrompue → ancien JSON, susceptible d'effacer pending et emergency stop. Downgrade JSON désormais refusé. |
| RPC/performance | `solana_ws` n'était pas admis par la liste de métriques et les tentatives endpoint/reprises n'étaient pas décomposées. Ajout de compteurs par tentative et benchmark local. |
| Backtest/comptabilité | SHA-256 démontre l'intégrité interne du dataset, pas l'authenticité de la source. Frais historiques Solana/Jupiter absents du modèle OHLC. |
| Tests/qualité | Pas de couverture mesurée ni de test d'arrêt brutal au départ. Ajout du test de processus enfant et de la collecte de couverture CI. |
| Sécurité indépendante | A confirmé le risque stale JSON et NaN/Inf du coupe-circuit (corrigés), le débit de token contrôlé après swap et l'arrêt d'urgence qui bloque aussi les ventes (limites restantes). |

## Rapport de sécurité indépendant et Jupiter

Voir [MISSION_6_SECURITY_REPORT.md](MISSION_6_SECURITY_REPORT.md). La garde ajoutée simule avant signature la transaction construite par Jupiter, demande `innerInstructions` et refuse une simulation en erreur/incomplète ou un programme CPI externe non allowlisté. L'allowlist est vide par défaut. Les tests sont synthétiques et n'appellent aucun RPC.

La garde vérifie l'identifiant observé et les index de compte, pas la sémantique de chaque instruction, les comptes autorisés par la logique métier, ni le code déployé du DEX. Un seul noeud RPC/slot ne garantit pas l'état lors d'une éventuelle exécution. La doc Solana confirme que `simulateTransaction` n'est pas diffusée et documente les instructions internes/métadonnées de frais; elle ne certifie pas les programmes appelés ([simulation](https://solana.com/docs/rpc/http/simulatetransaction), [structures JSON](https://solana.com/docs/rpc/json-structures), [getTransaction](https://solana.com/docs/rpc/http/gettransaction)). **LIVE demeure bloqué.**

Le contrôle du débit exact de token intervient après confirmation. S'il échoue, il bloque l'intégration locale et laisse la signature à rapprocher, mais ne peut annuler l'effet on-chain. Les tokens à frais de transfert ne sont pas validés avant swap. Aucun renvoi automatique d'une signature incertaine n'est permis; une réussite finalisée sans position durable reste à rapprocher.

## Rapport récupération et SQLite

Voir [MISSION_6_RECOVERY_REPORT.md](MISSION_6_RECOVERY_REPORT.md). Épreuves isolées : processus enfant qui commit une signature puis sort par `os._exit`; refus du JSON stale après corruption de l'état SQLite commité; échec d'écriture SQLite déclenché par trigger laissant l'ancien commit lisible; pannes RPC injectées localement; 19 réouvertures/lectures SQLite vérifiées. La réconciliation reste read-only.

Une panne matérielle, une restauration de base en production, une coupure au milieu de chaque frontière SQLite, ou un basculement multi-région n'ont pas été simulés. Les frais finalisés en échec sont conservés en lamports, pas traduits automatiquement en USD historique.

## Soak PAPER et benchmark reproductible

Voir [MISSION_6_BENCHMARK.md](MISSION_6_BENCHMARK.md), script [mission6_paper_benchmark.py](../scripts/mission6_paper_benchmark.py) et les mesures brutes [mission6-paper-soak.json](benchmarks/mission6-paper-soak.json). Commande :

```powershell
python scripts/mission6_paper_benchmark.py --cycles 10000 --output docs/benchmarks/mission6-paper-soak.json
```

Toutes les requêtes vont aux serveurs loopback `127.0.0.1` et ne demandent que `getHealth`. Le script n'utilise pas le `main.py` complet ni le réseau externe; il emploie une SQLite temporaire. Les latences p50/p95/p99 sont donc des mesures synthétiques locales et non des latences Solana.

## Backtest, frais et données

Voir [MISSION_6_BACKTEST_ACCOUNTING.md](MISSION_6_BACKTEST_ACCOUNTING.md). Le format OHLC valide intégrité et hash; l'origine fournisseur reste fournie par l'appelant, sans reçu signé. Le PnL historique n'inclut pas tous les frais réseau, route, échec, loyers et prix SOL/USD historiques du slot. Les chiffres historiques restent indicatifs.

## Tests, couverture et dépendances

- Tests complets : 378 réussis, 0 échoué, exécutés avec `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`.
- Compilation : `python -m compileall -q .` — réussie.
- `git diff --check` — réussi.
- Couverture: `coverage` absent localement; collecte ligne/branche ajoutée à la CI, rapport/JSON/XML téléversés. Valeur effective en attente de la CI de la tête finale.
- Ruff, pip-audit absents localement; la CI est configurée pour exécuter lint, audit des dépendances et Gitleaks. Aucun résultat CI final n'est revendiqué avant son observation.
- Le Skill `web-research-problem-solver` était absent du catalogue local; les faits externes sont liés directement aux docs officielles Solana et F Project.

La suite locale pytest sans `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` est bloquée avant discovery par un plugin global Web3 qui importe `ContractName` depuis `eth_typing`; avec l'autoload plugins désactivé, tous les tests passent.

## Comparaison F Project réellement vérifiable

Le Playbook public déclare 80 % d'exécution « Block 0 », cinq serveurs/3 continents, une limite jusqu'à 30 ruggers par wallet et plusieurs fonctions (copy trading, TP/SL, Buy The Dip, Rugger Protection) ([source officielle](https://f-project-1.gitbook.io/f-project-playbook)); le site décrit aussi le block zero et des wallets custodial ([site officiel](https://www.fproject.trade/)). Ce sont des affirmations de fournisseur. Les pages ne fournissent pas de traces brutes, méthode/dénominateur du 80 %, percentiles, taux d'échec défini, délais de reprise ou frais réseau mesurés indépendamment. Nos métriques sont locales synthétiques. Aucun classement comparative de vitesse, fiabilité, sûreté ou rentabilité n'est possible.

## Score et limites restantes

Le score de [PROGRESS.md](../PROGRESS.md) reste **72,5 %** selon le barème déjà publié. Les nouvelles preuves renforcent des éléments déjà notés `TESTED`, mais ne ferment pas l'audit sémantique des programmes DEX, le soak PAPER sur l'environnement cible, l'authenticité OHLC, le PnL historique tous frais compris, ou la reprise sur déploiement réel. Cette estimation mesure l'avancement d'implémentation, pas la préparation à utiliser des fonds.

## Restrictions respectées

- Worktree `main` et modifications locales préexistantes préservés.
- PR #1–#5 inchangées et non fusionnées.
- PAPER obligatoire; opt-in LIVE désactivé.
- Aucune transaction réelle signée ou envoyée.
- Aucun secret affiché ou commité; le harnais et la CI désactivent le chargement dotenv.
- Aucune statistique F Project non publiée inventée.
