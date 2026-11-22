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

Elle téléverse `coverage.json` et `coverage.xml`. Sur la tête `3c4307b2ab5823d116a698a0ed73d13eb9d3f190`, la CI a confirmé **378 tests réussis** et les valeurs exactes tirées de `coverage.json` sont :

- Lignes : **3 226 / 10 116 = 31,89 %**.
- Branches : **922 / 3 732 = 24,71 %**.
- Couverture totale combinée rapportée par Coverage.py : **29,95 %**.
- [Run CI 45064722874](https://github.com/cheicki225/-meme-coin/actions/runs/45064722874), job `validate` réussi; artefact `mission-coverage-45064722874` (ID `14770472989`).

Ces valeurs couvrent le code Python inclus par `--source=.` avec les exclusions montrées plus haut. Les fichiers de tests et environnements virtuels sont exclus. Ce taux ne signifie pas que 70 % des parcours critiques de trading sont validés : les lacunes métier et d'exploitation figurent dans les rapports ci-dessous.

Les essais de pannes sont décrits dans [le rapport récupération](MISSION_6_RECOVERY_REPORT.md) et [le rapport sécurité](MISSION_6_SECURITY_REPORT.md).
