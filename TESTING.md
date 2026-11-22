# Tests, contrôles et limites

## Vérification locale

Depuis la racine du dépôt :

```powershell
python -m compileall -q .
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -m pytest -q
```

Dans l'environnement local de cette revue, un plugin pytest global incompatible avec `eth_typing` empêche le chargement automatique normal. La désactivation ci-dessus permet de charger les tests du projet sans modifier l'environnement global. La CI GitHub n'utilise pas cette option.

Dernière vérification locale : `python -m compileall -q .` réussi, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q` exécuté le 22 novembre 2026 avec **196 tests réussis**, et `git diff --check` sans erreur. Les avertissements affichés par Git concernent uniquement la conversion de fins de ligne LF vers CRLF.

## CI configurée

Le workflow `.github/workflows/ci.yml` installe les dépendances, compile les sources, vérifie le mode PAPER par défaut, exécute pytest, Ruff sur les erreurs critiques, `pip-audit` et Gitleaks. Le run GitHub Actions **#198** sur le commit de code `8e6df9f` est vert, avec toutes ces étapes réussies. Ruff, pip-audit et Gitleaks ne sont pas installés dans l'environnement local actuel; ils ont été exécutés par la CI. L'installation locale des outils a échoué à la vérification TLS du dépôt Python, sans contournement de TLS.

## Portée des tests

- Jupiter : ordres adversariaux, metadata, transactions, ALT/ATA, exécution simulée et comptabilité avec fixtures; aucune transaction signée n'est diffusée.
- RPC : retry 429/5xx, `Retry-After`, jitter et redaction des messages; aucun test de charge ou de disponibilité du fournisseur déployé.
- Wallet : idempotence SQLite, curseur, collecte publique mockée et distinction entre activité vide et panne RPC; aucun historique réel du déploiement utilisé.
- SQLite/reprise : tests de restauration/schéma/signatures pendantes; pas d'exercice avec la base de production.
- PnL/risque : fixtures de balance et frais; pas de performance mesurée en marché.
- Backtest : OHLC synthétique; pas de dataset historique vérifié, ni de validation sans lookahead de bout en bout.

## États

`NOT STARTED`, `IN PROGRESS`, `IMPLEMENTED`, `TESTED`, `VERIFIED` et `BLOCKED` distinguent le code écrit des preuves disponibles. Le taux de couverture lignes/branches n'est pas mesuré dans cette révision.
