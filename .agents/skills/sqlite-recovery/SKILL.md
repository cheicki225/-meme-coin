---
name: sqlite-recovery
description: "Review or change SQLite state persistence, legacy JSON import, backups, crash recovery, or pending-transaction recovery."
---

# Récupération SQLite

## Déclencheurs

Utilise ce skill pour les incidents ou changements concernant SQLite, les sauvegardes d’état, l’import historique JSON, la reprise après arrêt brutal, les migrations de schéma, l’arrêt d’urgence persistant ou les signatures pendantes.

## Audit avant toute modification

- Lire l’état Git et le diff avant de modifier quoi que ce soit; identifier le checkout et la branche concernés.
- Examiner [sqlite_state.py](../../../sqlite_state.py), [monitoring_list.py](../../../monitoring_list.py), [execution_recovery.py](../../../execution_recovery.py), les appels de reprise dans main.py et les chemins de stockage déclarés dans config.py.
- Confirmer les versions et chemins de stockage effectivement codés; la configuration par défaut ne prouve pas le backend ou les chemins utilisés par un déploiement.
- Lire tests/test_sqlite_state.py, tests/test_datastore_recovery.py, tests/test_execution_recovery.py et tests/test_emergency_stop_persistence.py. Vérifier que les tests ciblés utilisent tmp_path ou remplacent explicitement leur stockage.

## Workflow

1. Reproduire chaque panne sur une base temporaire. Distinguer corruption SQLite, erreur de schéma, verrou, permission, espace disque et erreur transitoire. Le repli automatique au démarrage doit rester fail-closed face à un SQLite engagé corrompu; ne pas le remplacer par un JSON ancien. Le dépôt expose aussi DataStore.restore_from_backup, une opération explicite qui accepte des sauvegardes SQLite ou JSON et crée un backup pré-restauration. Ne l'exercer que sur des fixtures temporaires; toute restauration opérationnelle exige une demande et un processus distincts.
2. Préserver la base d’origine et ses fichiers WAL/SHM. Toute inspection d’une base existante se fait sur une copie cohérente. Ne supprime pas de fichiers pour « réparer » SQLite.
3. Vérifier le comportement de SQLiteStateStore: WAL, synchronous FULL, integrity_check, version de schéma, JSON strict et sauvegarde SQLite cohérente. Tester qu’un échec d’écriture conserve le dernier état commis.
4. Garder l’import du JSON historique limité au cas où la base SQLite n’a pas encore d’état commis. Une base commise mais corrompue ne doit pas être remplacée par un JSON ancien. Ne jamais abaisser une version de schéma plus récente.
5. Dans le parcours de récupération, préserver les signatures pendantes, l’arrêt d’urgence et l’état non résolu. Un résultat inconnu n’autorise ni retransmission ni suppression d’une intention.
6. Ajouter une simulation de panne isolée pour toute nouvelle frontière de crash ou transition de reprise; vérifier l’état avant arrêt, après redémarrage et après erreur d’écriture.

## Sécurité

N’ouvre, ne copie ni ne modifie la base opérationnelle du bot dans ce travail. N’affiche pas son contenu, ses clés, signatures complètes, soldes ou identifiants privés. Travaille avec une base temporaire et des fixtures fictives.

PAPER reste la valeur par défaut. N’envoie aucune transaction, ne signe rien, n’effectue aucun appel RPC vers un réseau réel et n’active jamais LIVE. Si un incident de production est rapporté, documente d’abord les éléments à préserver et les vérifications de lecture seule; le Skill ne donne pas l’autorisation de restaurer le service.

## Tests

Exécuter au minimum les tests ciblés de SQLite, restauration, reprise des signatures et arrêt d’urgence. Le dépôt possède déjà des cas pour le JSON mal formé ou dupliqué, la corruption, les erreurs transitoires, le schéma plus récent, la survie d’un commit à os._exit et le refus du downgrade vers une ancienne sauvegarde JSON.

Utiliser uniquement des fichiers sous tmp_path. Pour les changements de crash/restart, employer un sous-processus de test avec une base jetable; ne jamais viser le chemin provenant de la configuration utilisateur.

## Preuves attendues

Indiquer le backend et le schéma testés, l’emplacement temporaire des fixtures, les pannes simulées, les états avant/après observés et les tests réellement exécutés. Distinguer une reprise démontrée par tests locaux d’une restauration sur le déploiement, qui exige une validation opérationnelle distincte.

## Limites

La version de schéma actuellement déclarée est 1; vérifier le code avant toute mise à jour. Les tests de panne ne couvrent pas tous les types de dommage disque ni une restauration du service déployé. L’import legacy, la récupération depuis un backup et une migration sont des chemins différents et ne doivent pas être confondus.

## Passages de relais

- Logique de signature inconnue, Jupiter et réconciliation: jupiter-transaction-security.
- Arrêt d’urgence, limites et protections: trading-risk-manager.
- PnL ou registre des frais: pnl-accounting.
- Tests, sous-processus et CI: python-testing-ci.
- Audit de reprise opérationnelle: production-readiness-auditor.