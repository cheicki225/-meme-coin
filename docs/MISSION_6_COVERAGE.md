# Mission 6 — couverture et qualité des tests

## Résultats vérifiés localement

- Suite complète : **378 tests réussis, 0 échoué**, via `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q`.
- Compilation : `python -m compileall -q .` réussie.
- Vérification whitespace : `git diff --check` réussie.
- Couverture lignes/branches : non calculée localement; le module Python `coverage` n'est pas installé.
- Ruff et pip-audit : absents de l'interpréteur local.

Ici, `pytest` sans `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` échoue avant découverte des tests sur un plugin global Web3 qui importe `ContractName` depuis `eth_typing`. La variable permet de lancer les tests du projet sans ce plugin tiers. `PYTHON_DOTENV_DISABLED=true` empêche aussi le chargement automatique d'un éventuel `.env` pendant les tests.

## Mesure automatique par la CI

La CI installe désormais `requirements-ci.txt` puis exécute :

```bash
coverage run --branch --source=. --omit='tests/*,test_*.py,.venv/*,venv/*' -m pytest -q
coverage report --precision=2
coverage json -o coverage.json
coverage xml -o coverage.xml
```

Elle téléverse `coverage.json` et `coverage.xml`. Après exécution de la CI sur la tête finale, inscrire ici les résultats vérifiables : `couverture lignes = <valeur CI>`, `branches = <valeur CI>`, `run CI = <lien>`. Les pourcentages sont absents tant que ce run n'a pas été observé; aucun résultat n'est estimé depuis le nombre de tests.

Les essais de pannes sont décrits dans [le rapport récupération](MISSION_6_RECOVERY_REPORT.md) et [le rapport sécurité](MISSION_6_SECURITY_REPORT.md).
