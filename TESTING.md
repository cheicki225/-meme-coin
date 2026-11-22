# Tests, contrôles et limites

## Vérification locale

Depuis la racine du dépôt :

```powershell
python -m compileall -q .
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -m pytest -q
```

Dans l'environnement local de cette revue, un plugin pytest global incompatible avec `eth_typing` empêche le chargement automatique normal. La désactivation ci-dessus permet de charger les tests du projet sans modifier l'environnement global. La CI GitHub n'utilise pas cette option.

Dernière vérification locale sur `codex/mission-performance-2026-11-22` : `python -m compileall -q .` réussi, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q` exécuté le 22 novembre 2026 avec **348 tests réussis**, et `git diff --check` sans erreur. Le head précédent de la PR #4 a eu la CI #203 verte; la CI de cette nouvelle branche est encore à lancer.

## CI configurée

Le workflow `.github/workflows/ci.yml` installe les dépendances, compile les sources, vérifie le mode PAPER par défaut, exécute pytest, Ruff sur les erreurs critiques, `pip-audit` et Gitleaks. Le run GitHub Actions **[#203](https://github.com/cheicki225/-meme-coin/actions/runs/45047919815)** est vert sur le head précédent `e234c731`; la nouvelle branche de mission n'a pas encore exécuté cette CI. Ruff et pip-audit ne sont pas disponibles dans l'environnement local; leur installation isolée a échoué à vérifier le certificat TLS de PyPI. Gitleaks n'est pas installé localement.

## Portée des tests

- Jupiter : ordres adversariaux, metadata, transactions, ALT/ATA, exécution simulée et comptabilité avec fixtures; aucune transaction signée n'est diffusée.
- Jupiter HTTP : les appels Order et Execute refusent les redirections; tests 302 avec sessions simulées, sans clé réelle ni transaction.
- RPC : retry 429/5xx, `Retry-After`, jitter et redaction des messages; aucun test de charge ou de disponibilité du fournisseur déployé.
- Performance : mesures de durée et outcome pour la détection, la décision, la stratégie, Jupiter et RPC; liste blanche des labels, voies primaire/repli RPC, erreurs de listener et percentiles testés sur données synthétiques, sans mesure d'exploitation.
- Réconciliation : inspection RPC injectable, échec finalisé avec frais, succès sans position durable, registre mal formé au redémarrage et erreurs de persistance simulés; aucun RPC externe n'est appelé par ces tests.
- Telegram : `/performance` est testé pour l'échappement des libellés et une lecture hors boucle asyncio; `/reconcile` et l'échec de persistance de `/halt` sont testés avec des adaptateurs simulés; aucun bot déployé n'a été contacté.
- Wallet : idempotence SQLite, curseur, collecte publique mockée et distinction entre activité vide et panne RPC; aucun historique réel du déploiement utilisé.
- SQLite/reprise : tests de restauration/schéma/signatures pendantes; pas d'exercice avec la base de production.
- PnL/risque : fixtures de balance et frais; pas de performance mesurée en marché.
- Backtest : OHLC synthétique; pas de dataset historique vérifié, ni de validation sans lookahead de bout en bout.

## États

`NOT STARTED`, `IN PROGRESS`, `IMPLEMENTED`, `TESTED`, `VERIFIED` et `BLOCKED` distinguent le code écrit des preuves disponibles. Le taux de couverture lignes/branches n'est pas mesuré dans cette révision.
