# Mission 8 — optimisation RPC/WebSocket

Date de l'audit : 22 novembre 2026. Statut : `MANUAL_VERIFICATION_REQUIRED`.

## État effectif de Mission 7

- PR #8, `codex/mission-7-reliability-2026-11-22`, est ouverte, non fusionnée et empilée sur PR #6. Head vérifié : `b8438734b00c18a265f0907e83cb5e7bff25a43f`.
- La CI GitHub du head M7, run [45074903163](https://github.com/cheicki225/-meme-coin/actions/runs/45074903163), est terminée avec succès : 468 tests, couverture combinée 31,08 %, pip-audit sans vulnérabilité connue et Gitleaks sans fuite détectée. Cette validation ne représente pas un soak d'exploitation.
- Restent non vérifiés : soak PAPER de 24 h dans l'environnement cible, exercice de restauration dans cet environnement, endpoint/quota du fournisseur réel, et revue de la sémantique ainsi que du code déployé des DEX. Le statut M7 est donc `INCOMPLETE / MANUAL_VERIFICATION_REQUIRED`; LIVE reste `BLOCKED`.
- PR #7 (skills) est une branche distincte, non fusionnée. Son catalogue indique que `web-research-problem-solver` est une aide externe conditionnelle non livrée; le skill n'est pas installé dans ce dépôt/session (`SKILL_UNAVAILABLE`). Aucun blocage technique résiduel n'a nécessité ce handoff pour le périmètre M8.

## Portée et pile Git

La branche dédiée `codex/mission-8-performance-2026-11-22` est empilée sur le head courant de PR #8. Les optimisations proposées portent sur le trafic de lecture, la gestion de quota/429, la déduplication de lectures simultanées, les métriques et la reconnexion WebSocket. Les tests ne soumettent aucune transaction. Aucun changement de limites de risque, de paramètres de trading, de signature ou d'opt-in LIVE n'est inclus. PAPER reste le défaut.

Le point de départ M7 n'ajoute que des rapports et des tests ciblés aux fichiers modifiés par M8; les gates opérationnelles M7 restent ouvertes. Les nouvelles lectures/sockets observées ici sont distinctes du fournisseur configuré au déploiement, qui reste inconnu.

## Changements préparés

- Limiteur par hôte avec espacement configurable par `SOLANA_RPC_REQUESTS_PER_SECOND` (défaut 3 requêtes/s, même valeur pour tous les hôtes RPC standard). Le point de terminaison Helius Enhanced reçoit un quota distinct `HELIUS_ENHANCED_REQUESTS_PER_SECOND` (défaut 1 requête/s, moitié du quota Free [actuellement publié](https://www.helius.dev/docs/billing/rate-limits)). Les valeurs sont appliquées séparément par hôte; elles doivent être configurées sous les quotas réels et ne les établissent pas. Les fréquences fractionnaires positives sont prises en charge.
- Les `429` enregistrent un cooldown partagé par hôte en mémoire. `Retry-After` est lu en secondes ou date HTTP, avec seulement un jitter positif. Pour une attente supérieure à 30 s, l'appel de lecture abandonne l'hôte pour cet appel afin d'essayer un secours configuré au lieu de bloquer. Les erreurs HTTP/transitoires des lectures gardent le retry borné.
- La répétition et le secours sont fermés par défaut. Seule une allowlist explicite de méthodes de lecture sans effet de bord peut être retentée/secourue; méthodes mutatrices ou inconnues ne reçoivent qu'un essai. Les méthodes de soumission usuelles `sendTransaction`, `sendRawTransaction` et `sendBundle` ne sont pas retentées et ne basculent pas vers le secours. Le comportement interne de relay/retry du fournisseur n'est pas établi par ce changement.
- Les lectures allowlistées strictement identiques sont coalescées uniquement tant que la requête est en vol, avec correspondance de l'hôte primaire, du contenu JSON-RPC et du timeout. Aucun résultat n'est mis en cache après la réponse.
- Le WebSocket utilise un backoff exponentiel avec jitter, borné à 30 s, après exception ou fermeture normale; le délai est remis à zéro après 60 s de connexion stable. `stop()` ferme le socket courant et réveille l'attente.
- Les métriques agrégées ajoutent les étapes d'attente de quota, lecture coalescée, parsing, callback, envoi des souscriptions et attente du backoff de reconnexion. Elles mesurent le backoff, pas le handshake du prochain WebSocket. Les étapes existantes decision/Jupiter/soumission/confirmation restent filtrées par liste blanche. Il n'y a pas de corrélation par événement entre détection et confirmation.

## Mesures publiques read-only

Les probes ont utilisé seulement des méthodes de lecture (`getSlot` confirmé et `slotSubscribe`) sur les endpoints publics, depuis l'hôte d'exécution Codex le 22 novembre 2026. Aucun fichier `.env`, secret, compte, wallet, signature, transaction, charge artificielle ni paramètre d'exécution du bot n'a été utilisé. La région de sortie de cet hôte n'est pas connue. Le résumé des mesures est dans [`benchmarks/mission8-public-rpc-readonly.json`](benchmarks/mission8-public-rpc-readonly.json).

| Probe HTTP (n=30 par endpoint) | Réussite | Échecs observés | p50 | p95 | p99 |
|---|---:|---:|---:|---:|---:|
| Solana public Mainnet | 21/30 | 9 timeouts (30 %) | 160,19 ms | 1 511,61 ms | 1 514,82 ms |
| PublicNode | 30/30 | 0/30 | 134,95 ms | 239,37 ms | 325,33 ms |

Chaque endpoint a reçu 30 lectures espacées d'environ 400 ms avec un timeout de 1,5 s. Pour p95/p99 sur un échantillon de 30, les valeurs de queue sont proches du maximum de l'échantillon et ne sont pas des estimations stables de production.

Un second échantillon de 10 paires simultanées de lectures `getSlot` réussies a observé un écart de slot médian de 1 et p95 de 4 (min 0, max 4). Cette mesure courte ne permet pas de qualifier un fournisseur comme plus frais en général.

Une connexion WebSocket à l'endpoint public Solana a reçu 30 notifications `slotSubscribe` : handshake 1 982,16 ms, accusé de souscription 490,63 ms, première notification 522,21 ms, intervalle entre notifications p50/p95/p99 268,66/603,04/659,64 ms. Ces intervalles ne sont ni une latence de détection ni une mesure de bout en bout. Sans séquence attendue indépendante, le taux d'événements manqués n'est pas calculable.

Limites : petit échantillon, un hôte/réseau et un instant, pas de charge ni de région cible, pas de mesure du pipeline applicatif complet. Les percentiles et taux d'erreur décrivent seulement ces probes; les traces brutes par requête ne sont pas archivées. Ne pas utiliser ces résultats pour choisir définitivement un fournisseur ou annoncer une performance LIVE.

Un HTTP 429 lors du parsing Enhanced active le cooldown partagé, mais la signature correspondante n'est pas conservée dans une file de reprise. Une perte d'événement reste possible jusqu'à la mise en place d'un rattrapage borné en lecture seule et sa validation.

## Comparaison publique avec F Project

Le [Playbook F Project](https://f-project-1.gitbook.io/f-project-playbook) présente des affirmations produit, notamment « 80 % Block 0 », cinq serveurs répartis sur trois continents et jusqu'à 30 wallets suivis par wallet. Les pages publiques consultées ne donnent pas de période, dénominateur, traces reproductibles, latences p50/p95/p99 ni taux d'échec comparable. Ces chiffres sont donc des affirmations du fournisseur, pas des benchmarks indépendants. Aucune supériorité ou équivalence mesurée du bot n'est démontrée.

## Validation et travaux encore requis

Validation locale M8 effectuée hors ligne, avec `TRADING_MODE=PAPER`, `LIVE_EXECUTION_ENABLED=false`, autochargement dotenv désactivé et sans clés/provider privé :

- `python -m compileall -q .` — réussi.
- `python -m coverage run --branch --source=. --omit='tests/*,test_*.py,.venv/*,venv/*' -m pytest -q` — **484 tests réussis**, 0 échec. Rapport de couverture combinée affiché : **31,88 %**.
- `python -m ruff check --select E9,F63,F7,F82 .` — réussi.
- `python -m pip_audit -r requirements-ci.txt` — **non validé localement** : l'outil a échoué lors de la mise à niveau de pip dans son environnement temporaire; aucun résultat propre d'audit n'est revendiqué. Gitleaks n'est pas installé localement. Les résultats CI M7 ci-dessus ne valent pas CI M8.

Restent également nécessaires, avant toute décision opérationnelle : mesure depuis l'environnement cible, quota réel fourni par l'opérateur, soak PAPER, réconciliation après reconnexion, rattrapage d'événements après HTTP 429, exercice de restauration et contrôles de déploiement. LIVE demeure bloqué tant que les gates de sécurité et d'exploitation documentées ne sont pas fermées.
