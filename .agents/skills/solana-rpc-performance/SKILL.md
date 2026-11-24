---
name: solana-rpc-performance
description: Audit or change this bot's Solana HTTP RPC, provider fallback, retry, rate-limit, WebSocket capability, or latency telemetry paths.
---

# Solana RPC performance

## Déclencheurs

Utiliser cette compétence lorsqu'une demande concerne `rpc_client.py`, les RPC publics, le fallback Alchemy, les réponses 429/5xx, le contrôle de débit, la latence ou la disponibilité des flux WebSocket/Hélius. Elle couvre l'instrumentation des chemins RPC existants; une demande d'architecture fournisseur à grande échelle doit aussi passer par `production-readiness-auditor`.

## Audit avant toute modification

- Lire `git status --short --branch`, le diff, la branche et la base PR active. Ne pas travailler depuis le checkout `main` existant lorsqu'il contient des changements locaux.
- Examiner `RPC_PUBLIC_LIMITATIONS.md`, `SECURITY.md`, `rpc_client.py`, `rpc_health_monitor.py`, `execution_metrics.py`, puis les consommateurs concernés (`wallet_history.py`, `wallet_data_collector.py`, `jupiter_executor.py` ou autres trouvés par recherche).
- Vérifier dans `config.py` les noms et la logique de sélection des variables `SOLANA_RPC_URL`, `HELIUS_API_KEY`, `ALCHEMY_API_KEY` et les URL dérivées, sans afficher ni journaliser leurs valeurs. Ne jamais ouvrir `.env`.
- Examiner `tests/test_rpc_retry_policy.py`, `tests/test_rpc_metrics.py`, `tests/test_rpc_health_monitor.py` et les tests des consommateurs concernés. Distinguer les attentes couvertes des lacunes.
- Établir si la demande touche HTTP RPC, Helius WebSocket, parsing enrichi, ou plusieurs voies; leurs capacités et prérequis ne sont pas interchangeables.

## Workflow

1. Décrire le chemin actuel depuis le consommateur jusqu'à `rpc_client.py`; repérer appels directs qui contournent le client centralisé avant de proposer un changement.
2. Préserver la sélection actuelle: `SOLANA_RPC_URL` peut remplacer le primaire, Helius est construit si sa clé est configurée, le RPC public Solana est le défaut sans ces deux valeurs, et Alchemy reste un fallback facultatif. Ne pas remplacer les RPC publics déjà configurés.
3. Pour toute optimisation, définir une mesure observable (compteur, erreur, tentative ou latence), la charge et les limites. Ne pas présenter la limite globale codée de 5 appels/s comme un quota réel du fournisseur.
4. Documenter la politique de retry au niveau de la méthode JSON-RPC. Dans le code actuel, rpc_post appelle un retry générique qui ne distingue pas sendTransaction; wallet_manager.py utilise cette voie et tests/test_rpc_retry_policy.py vérifie un retry après HTTP 429. Ne pas affirmer que les soumissions sont exclues des retries. Pour une transaction, tracer le chemin exact et traiter ce comportement comme un risque connu; transmettre le chemin wallet à multi-wallet-execution et le chemin Jupiter V2 à jupiter-transaction-security. Toute modification de cette politique exige un périmètre explicite et des tests d'idempotence/refus dédiés.
5. Garder les métriques agrégées et les labels contrôlés. Éviter les journaux ou sorties contenant URL, query string, clé, réponse brute, signature, wallet, mint ou contenu sensible.
6. Séparer disponibilité HTTP publique de Helius WebSocket et du parsing enrichi: l'absence de clé Helius désactive ces dernières voies dans la version actuelle.

## Sécurité

- Les appels externes de vérification doivent rester hors périmètre, sauf demande explicite; privilégier mocks HTTP et fixtures locales.
- N'envoyer aucune transaction, ne signer aucune donnée et ne pas activer LIVE. Les méthodes de lecture ne doivent pas être détournées vers une soumission.
- Ne pas divulguer les URL complètes des fournisseurs: les chemins et query strings peuvent contenir des credentials.
- Toute modification de configuration conserve PAPER comme mode par défaut et les RPC publics actuels.

## Tests

- Tester en isolation avec sessions HTTP simulées les erreurs transitoires, `Retry-After`, erreurs permanentes, fallback optionnel et redaction des journaux/métriques.
- Utiliser `tests/test_rpc_retry_policy.py`, `tests/test_rpc_metrics.py` et `tests/test_rpc_health_monitor.py`, plus les tests du consommateur modifié.
- Pour un changement observable de comportement, ajouter des cas d'échec et de récupération sans accéder au réseau. Ne pas qualifier des tests simulés de test RPC réel.

## Preuves attendues

Rapporter les fichiers inspectés/modifiés, la commande exacte et la sortie des tests exécutés, les métriques produites sur la charge indiquée, et si l'évidence est simulée, HTTP lecture seule, ou issue d'un environnement PAPER cible. Nommer les fournisseurs et quotas qui restent non vérifiés sans révéler leurs URL ou clés.

## Limites

Les RPC publics sont mutualisés et leurs quotas peuvent changer. Les tests HTTP simulés ne mesurent ni latence, ni fraîcheur, ni disponibilité en production. Les valeurs d'environnement de déploiement sont inconnues. Une voie WebSocket indisponible n'est pas remplacée automatiquement par le RPC HTTP.

## Passages de relais

- `wallet-intelligence-collector` pour les lectures d'historique, limites de collecte et curseurs.
- `jupiter-transaction-security` pour les RPC de simulation et toute analyse de transaction Jupiter.
- `telegram-monitoring` pour les alertes de santé et leur exposition opérateur.
- `python-testing-ci` pour l'isolation des tests, le lint et les contrôles CI.
- `production-readiness-auditor` pour les quotas, fraîcheur, secours et mesures dans l'environnement cible.
