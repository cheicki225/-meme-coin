# Mission 6 — rapport récupération et persistance

Base : commit PR #5 `7fe6db45b1d661738a38006bbd8278bfc62b7e06`; tests dans des dossiers temporaires isolés.

## Problème reproduit et correction

Sur une copie temporaire, l'audit a reproduit le cas suivant : la base SQLite contient un état commité avec `emergency_stop=true` et une transaction signée en attente; le contenu SQLite devient corrompu; aucune sauvegarde SQLite valide n'existe; une ancienne sauvegarde JSON n'inclut ni le pending ni le stop. Avant correction, ce JSON pouvait remplacer le contenu SQLite commité.

`DataStore._load` refuse maintenant de restaurer/importer une sauvegarde JSON quand la base SQLite présente un état commité que les backups SQLite n'ont pas réussi à récupérer. Il lève une erreur et fait échouer le chargement, évitant un démarrage depuis un snapshot périmé. Une sauvegarde SQLite cohérente reste la voie de récupération prioritaire.

## Exercices exécutés

| Scénario isolé | Résultat observé |
|---|---|
| Enfant écrit/commit une signature pending, puis quitte avec `os._exit`; le parent reconstruit DataStore | Signature conservée; recovery active l'arrêt et ne retransmet rien. |
| SQLite engagée corrompue + JSON ancien sans pending ni arrêt | JSON de secours refusé; état non récupérable arrête le chargement. |
| Trigger SQLite d'abandon sur UPDATE | Sauvegarde en erreur; la dernière valeur commitée reste lisible après réouverture. |
| RPC 429 temporaire injecté | Retry borné puis réponse réussie au serveur local. |
| RPC primaire 503 injecté | Retry borné; endpoint local de secours répond pour les cas configurés. |
| RPC primaire et secours 503 | Requête en échec; aucune action de transaction. Pending conservé au redémarrage testé séparément. |
| Réouverture SQLite au cours du soak PAPER synthétique | 19 redémarrages vérifiés aux cycles 500 à 9500; état `cycles=10000` relu en fin d'exécution. |

Le trigger simule une erreur d'écriture au niveau SQLite, pas une panne physique de disque. L'arrêt brutal est testé immédiatement après commit; les frontières pré-commit, panne de contrôleur/disque, basculement régional, absence de backup sur l'hôte réel et restauration d'un déploiement ne sont pas mesurés.

## Réconciliation transactionnelle

La réconciliation ne renvoie aucune transaction. Elle ne classe « finalisé-échec » qu'après lecture d'un statut finalisé et des métadonnées; signatures absentes, non finalisées ou incomplètes restent pending/unknown. Une réussite finalisée sans position durable reste pending. Une erreur de sauvegarde maintient l'arrêt et conserve le pending en mémoire.

Les frais d'échecs finalisés restent stockés en lamports en attente d'application. Ils ne sont pas convertis automatiquement en USD historique ni intégrés de façon complète au PnL passé. Les tests de recovery sont locaux, RPC simulé.

## Commandes et résultats

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest -q tests/test_datastore_recovery.py tests/test_execution_recovery.py tests/test_sqlite_state.py tests/test_emergency_stop_persistence.py
```

Résultat ciblé : **36 passed**. Suite complète après toutes les modifications : **378 passed, 0 failed**. Compilation et `git diff --check` réussis.

## Limite et action restante

Corruption sans backup SQLite valide échoue maintenant de façon fermée et exige restauration/examen opérateur. La récupération continue, disponibilité multi-région, backup hors hôte et coupure électrique réelle ne sont pas validés. Ne pas considérer la restauration en production comme terminée.
