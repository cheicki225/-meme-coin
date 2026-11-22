# Tests, contrôles et limites

## Vérification locale

Depuis la racine du dépôt :

```powershell
python -m compileall -q .
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:PYTHON_DOTENV_DISABLED = 'true'
python -m pytest -q
```

Dans l'environnement local de cette revue, un plugin pytest global incompatible avec `eth_typing` empêche le chargement automatique normal. La désactivation ci-dessus permet de charger les tests du projet sans modifier l'environnement global. La CI GitHub n'utilise pas cette option.

Dernière vérification locale sur `codex/mission-performance-2026-11-22` : `python -m compileall -q .` réussi, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q` exécuté le 22 novembre 2026 avec **357 tests réussis**, et `git diff --check` sans erreur. La CI GitHub [45059131294](https://github.com/cheicki225/-meme-coin/actions/runs/45059131294) est verte sur `a30f11a` : installation, compilation, défaut PAPER, pytest (357), Ruff, pip-audit (aucune vulnérabilité connue) et Gitleaks (aucun secret détecté).

## CI configurée

Le workflow `.github/workflows/ci.yml` installe `requirements-ci.txt`, compile les sources, vérifie PAPER par défaut, exécute pytest avec Coverage.py lignes/branches, Ruff sur les erreurs critiques, `pip-audit` sur les exigences CI et Gitleaks. `coverage.json` et `coverage.xml` sont téléversés comme artefacts. La CI [45059131294](https://github.com/cheicki225/-meme-coin/actions/runs/45059131294) est verte sur le commit `a30f11a`; les 357 tests, Ruff, l'audit des dépendances et le scan de secrets y ont réussi. La mesure de couverture de Mission 6 doit être lue sur son propre run.

## Portée des tests

- Jupiter : ordres adversariaux, metadata, transactions, ALT/ATA, exécution simulée et comptabilité avec fixtures; aucune transaction signée n'est diffusée.
- Jupiter HTTP : les appels Order et Execute refusent les redirections; tests 302 avec sessions simulées, sans clé réelle ni transaction.
- RPC : retry 429/5xx, `Retry-After`, jitter et redaction des messages; aucun test de charge ou de disponibilité du fournisseur déployé.
- Performance : mesures de durée et outcome pour la détection, la décision, la stratégie, Jupiter et RPC; liste blanche des labels, voies primaire/repli RPC, erreurs de listener et percentiles testés sur données synthétiques, sans mesure d'exploitation.
- Réconciliation : inspection RPC injectable, échec finalisé avec frais, succès sans position durable, registre mal formé au redémarrage et erreurs de persistance simulés; aucun RPC externe n'est appelé par ces tests.
- Telegram : `/performance` est testé pour l'échappement des libellés et une lecture hors boucle asyncio; `/reconcile` et l'échec de persistance de `/halt` sont testés avec des adaptateurs simulés; aucun bot déployé n'a été contacté.
- Wallet : idempotence SQLite, curseur, collecte publique mockée, distinction activité vide/panne RPC, alerte sur collecte partielle, reprise du curseur après échec de sauvegarde et lecture limitée de l'historique; `/activity` n'affiche que les wallets surveillés et reste en lecture seule; aucun historique réel du déploiement utilisé.
- SQLite/reprise : tests de restauration/schéma/signatures pendantes; pas d'exercice avec la base de production.
- PnL/risque : fixtures de balance et frais; pas de performance mesurée en marché.
- Backtest : OHLC synthétique, frais/slippage et drawdown mark-to-market intratrade avec chemin conservateur déclaré; pas de dataset historique vérifié, ni de validation sans lookahead de bout en bout.
- Jupiter comptabilité : fixture teste le débit exact-in confirmé pour les tokens non SOL; les tokens à frais de transfert restent volontairement bloqués et non modélisés.

## États

`NOT STARTED`, `IN PROGRESS`, `IMPLEMENTED`, `TESTED`, `VERIFIED` et `BLOCKED` distinguent le code écrit des preuves disponibles. Le taux de couverture lignes/branches n'est pas mesuré dans cette révision.
# Mission 6 — mesure de couverture

La CI installe les outils de test depuis `requirements-ci.txt` et exécute pytest sous Coverage.py avec mesure des branches. Elle affiche `coverage report` et conserve `coverage.json`/`coverage.xml` comme artefacts. Le taux mesuré dépend du commit et doit être lu sur le run associé; le résultat local Mission 6 est **378 tests réussis**, mais la couverture n'a pas été installée dans l'interpréteur local et aucun pourcentage n'est donné sans preuve CI.

Pour reproduire le nombre de tests sur un poste qui possède des plugins pytest globaux incompatibles avec les dépendances locales :

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$env:PYTHON_DOTENV_DISABLED='true'
python -m pytest -q
```

Le soak synthétique est documenté dans [docs/MISSION_6_BENCHMARK.md](docs/MISSION_6_BENCHMARK.md). Il ne remplace pas un soak réel du déploiement.
