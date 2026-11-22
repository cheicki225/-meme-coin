---
name: wallet-intelligence-collector
description: Review read-only wallet history, monitored-wallet collection, activity classification, SQLite cursors, or candidate data boundaries.
---

# Intelligence de wallets et collecteur

## Déclencheurs

Utiliser cette compétence pour l'historique des développeurs/wallets, les événements d'activité, l'ajout et la collecte de wallets surveillés, les limites de scan, la déduplication ou les curseurs SQLite. Elle sépare l'observation historique du copy trading temps réel.

## Audit avant toute modification

- Inspecter branche, état Git et diff avant de modifier. Ne pas utiliser un checkout `main` modifié.
- Lire `WALLET_INTELLIGENCE.md`, `SECURITY.md`, `wallet_history.py`, `wallet_data_collector.py`, `wallet_collector_service.py`, `fund_tracer.py`, `find_dev.py` et leurs appels.
- Vérifier le schéma SQLite du collecteur, sa clé d'idempotence, le curseur, les bornes de `limit` et la séparation d'avec le magasin d'état principal.
- Lire `tests/test_wallet_data_collector.py`, `tests/test_wallet_collector_service.py` et les tests de `wallet_history.py` trouvés par recherche.
- Si la demande concerne `copytrade_listener.py`, délimiter le flux temps réel et passer le traitement d'ordre à `copy-trading`; ne pas supposer que les événements du collecteur alimentent une transaction.

## Workflow

1. Suivre le chemin depuis le wallet surveillé jusqu'à `getSignaturesForAddress`/`getTransaction` ou l'API enrichie, puis jusqu'à la normalisation et au stockage. Identifier l'appel RPC strict et le comportement d'erreur.
2. Distinguer les deux curseurs. Le curseur de signatures par wallet n'avance que si les fetchs de transactions du cycle sont complets; un résultat partiel le laisse inchangé. L'index de rotation wallet_collector_next_index, lui, avance du nombre de wallets sélectionnés même si un résultat est partiel/en erreur; seule une erreur de sauvegarde restaure sa valeur précédente en mémoire. Préserver ces deux propriétés et tester séparément leur reprise.
3. Traiter un historique vide avec succès comme différent d'une panne RPC. Les erreurs et pages partielles doivent rester visibles comme `error`/`partial`, pas être transformées en activité nulle.
4. Respecter la classification en candidat: les deltas de token/SOL autour d'un programme Pump.fun sont heuristiques; les événements peuvent être `unclassified_movement` et le PnL réalisé, prix d'entrée/sortie peuvent rester inconnus.
5. Garder le stockage d'activité séparé du calcul de PnL et des décisions de trading. Une donnée collectée ne doit pas déclencher directement un achat ni contourner les filtres d'entrée.
6. Vérifier l'idempotence (wallet, signature, mint), la reprise après redémarrage, la politique de rétention et le caractère privé des journaux/rapports avant d'élargir la collecte.

## Sécurité

- Le collecteur reste en lecture seule. Ne jamais signer, diffuser, copier une transaction, effectuer un transfert ni activer LIVE dans ce workflow.
- Le mode d'activation actuel est optionnel et désactivé par défaut; toute modification de ses limites ou de ses destinataires nécessite une justification explicite.
- N'exposer aucune clé API, clé privée, URL RPC avec credential, historique complet, signature ou portefeuille surveillé dans les métriques et rapports publics.
- Ne pas contacter Helius ou un autre fournisseur durant un test local sans autorisation explicite; utiliser des mocks et une base temporaire.

## Tests

- Utiliser `tests/test_wallet_data_collector.py` pour les deltas agrégés, l'idempotence, la persistance du curseur, la limite de lecture, la panne RPC, les résultats partiels et l'historique vide valide.
- Utiliser `tests/test_wallet_collector_service.py` pour la rotation bornée, les erreurs fournisseur, l'alerte partielle, l'échec de sauvegarde et les reprises.
- Couvrir les redémarrages et modifications de schéma avec une base SQLite temporaire; jamais avec la base utilisateur ou opérationnelle.

## Preuves attendues

Indiquer fichiers et tables inspectés, limites de collecte, comportement du curseur, tests exécutés et emplacement temporaire de toute base utilisée. Distinguer historique simulé, lecture seule RPC et observations du collecteur déployé. Signaler explicitement les événements non classés ou l'absence de PnL fiable.

## Limites

Les deltas de soldes ne prouvent pas à eux seuls qu'un swap a eu lieu; transfert, wrap/unwrap, frais, comptes intermédiaires et agrégateurs compliquent la classification. Le collecteur ne calcule pas le coût de base historique ni un PnL réalisé fiable et n'alimente pas les entrées de trade. Les WebSockets et transactions enrichies dépendent de la configuration Helius actuelle.

## Passages de relais

- `copy-trading` pour un usage des événements qui souhaite influencer une exécution; maintenir cette frontière explicite.
- `sqlite-recovery` pour la persistance, le schéma, les reprises et les curseurs durablement engagés.
- `telegram-monitoring` pour commandes de lecture et alertes de collecte.
- `solana-rpc-performance` pour quotas, retry et choix des transports.
- `python-testing-ci` pour les fixtures, bases temporaires et tests d'idempotence.
