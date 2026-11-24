---
name: python-testing-ci
description: "Run or repair this repository’s pytest, coverage, lint, dependency-audit, and GitHub Actions validation workflow."
---

# Tests Python et CI

## Déclencheurs

Utilise ce skill quand la demande porte sur pytest, les fixtures, la couverture, le lint, l’audit des dépendances, Gitleaks ou le workflow GitHub Actions. Pour un défaut financier précis, demande aussi le Skill du domaine concerné afin que la suite teste le bon invariant.

## Audit avant toute modification

- Lire l’état Git, la branche, le diff local et les instructions du dépôt; ne jamais écraser des changements existants.
- Vérifier la configuration réellement exécutée dans [.github/workflows/ci.yml](../../../.github/workflows/ci.yml), [requirements-ci.txt](../../../requirements-ci.txt), [TESTING.md](../../../TESTING.md) et les tests du domaine.
- Confirmer l’interpréteur Python et les dépendances sans afficher de valeurs d’environnement sensibles. La CI de ce dépôt utilise Python 3.11.
- Identifier si un test importe un module qui démarre un client, lit un fichier opérationnel ou contacte un service; remplacer ces frontières par des fixtures et mocks.

## Workflow

1. Reproduire le défaut avec le plus petit test déterministe possible, à partir d’un cas déjà observé ou d’un invariant de sécurité explicite.
2. Utiliser des chemins temporaires pour SQLite, JSON, métriques et sorties de benchmark. Réinitialiser les variables de mode et désactiver le chargement automatique de dotenv avec PYTHON_DOTENV_DISABLED=true.
3. Pour une modification locale ciblée, exécuter d’abord le test correspondant. Pour déclarer une validation complète, suivre les étapes configurées dans le workflow CI:
   - python -m compileall -q .
   - pytest avec Coverage.py lignes et branches
   - ruff check --select E9,F63,F7,F82 .
   - pip-audit -r requirements-ci.txt
   - Gitleaks via l’action GitHub configurée
4. Le test pytest global du poste peut être perturbé par un plugin externe incompatible. N’utiliser PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 que si l’échec de collecte est attribué à ce plugin, conformément à TESTING.md; signaler cette différence et ne pas confondre ce contournement local avec le résultat CI.
5. Si une dépendance ou une étape CI change, vérifier son usage dans la configuration avant d’ajouter un nouveau paquet. Garder les tests indépendants des comptes, des RPC réels et des services Telegram.
6. Après une correction, exécuter les tests du module puis la validation plus large pertinente. Ne transforme pas un taux de couverture en preuve de correction.

## Sécurité

Les tests demeurent PAPER et hors ligne. Ils ne doivent jamais charger une clé réelle, démarrer une signature, soumettre une transaction, contacter un bot Telegram réel ou activer LIVE. Un test réseau éventuel doit être simulé ou relié à un serveur de boucle locale; ne jamais utiliser une URL RPC configurée par l’utilisateur.

N’ajoute pas de secret, donnée de compte, wallet privé ou réponse fournisseur brute aux fixtures, logs, artefacts ou snapshots. Garde les tests dangereux opt-in et ignorés en l’absence d’un environnement explicitement autorisé.

## Tests

Les tests du dépôt sont dans tests/. Choisir les modules par le chemin touché: tests/test_jupiter_*, tests/test_risk_controls.py, tests/test_copytrade_listener.py, tests/test_execution_recovery.py, tests/test_datastore_recovery.py, tests/test_sqlite_state.py, tests/test_execution_metrics.py ou tests/test_telegram_ops_commands.py.

La validation configurée utilise coverage, pytest, Ruff et pip-audit déclarés dans requirements-ci.txt. Le rapport Gitleaks provient de l’action GitHub et doit être vérifié dans le run correspondant si elle n’a pas été exécutée localement.

## Preuves attendues

Dans le rapport, donner la commande réellement exécutée, la version Python, le résultat chiffré, les éventuels tests ignorés, et le commit/run CI auquel appartient chaque preuve. Séparer compilation, suite locale, couverture, lint, audit de dépendances et scan de secrets. Ne jamais annoncer un test ou une CI verte en se fondant sur la configuration seule.

## Limites

Les tests mockés prouvent le comportement face aux fixtures, pas la disponibilité des fournisseurs, la confirmation Solana, une exploitation continue, ni la sûreté globale d’un trade. Le run de CI varie selon le commit. La couverture n’est pas une mesure de rendement ni une approbation LIVE.

## Passages de relais

- Persistance et pannes de stockage: sqlite-recovery.
- Transactions Jupiter/CPI: jupiter-transaction-security.
- Contrôles financiers: trading-risk-manager.
- Latence et benchmark: solana-rpc-performance et f-project-benchmark.
- Telegram et alertes: telegram-monitoring.