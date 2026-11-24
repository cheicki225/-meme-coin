# Mission 8.5 — Helius Event Reliability & End-to-End Observability

## Stat

`PARTIALLY_READY` — les protections et scénarios hors ligne ciblés sont présents; la couverture CI du head final et la validation opérationnelle dans l’environnement cible restent requises. Ce rapport ne qualifie pas le bot pour LIVE.

Branche dédiée : `codex/mission-8-5-helius-observability-2026-11-24`, créée dans le clone `meme coin-mission8.5`. Elle part du head de la PR #11 (`2d0df1e9014e568e4fd4efd83308358a0e505721`) et intègre la PR #10 (`2007b6618d426ce9edd3adb501a598d2022a8a61`) comme dépendance. Les branches et PR existantes restent inchangées et non fusionnées.

## Changements

- `helius_event_store.py` fournit une inbox SQLite en WAL avec `synchronous=FULL`, unicité durable par flux/signature, ID de corrélation aléatoire, réessais bornés et checkpoint séquentiel. Le checkpoint avance uniquement après le retour réussi du gestionnaire.
- Les notifications sont persistées avant la récupération Enhanced. Un HTTP 429, une réponse vide ou une panne de lecture laisse l'événement en retry; les doublons WS/rattrapés réutilisent la ligne persistée.
- Le volume en attente est limité par nombre d'événements (`HELIUS_EVENT_QUEUE_MAX_PENDING`) et taille cumulée des payloads (`HELIUS_EVENT_QUEUE_MAX_BYTES`). À saturation, le refus est compté, le flux est marqué pour rattrapage et la socket est fermée pour appliquer la backpressure.
- Le rattrapage utilise `getSignaturesForAddress` en lecture seule, exige de retrouver le checkpoint, insère les événements les plus anciens d'abord et reste bloqué si l'historique est invalide, trop long ou ne rejoint pas le checkpoint.
- Les premières souscriptions créent un baseline de flux sans faux gap initial. Les erreurs d'ack/souscription et notifications WS mal formées marquent les flux actifs pour rattrapage et ferment la socket.
- Les événements dont le handler a commencé un effet potentiellement visible sont marqués avant le callback. Si le bail expire après cette frontière, l'événement passe en `UNKNOWN`, bloque le flux et demande une revue manuelle; aucun rejeu aveugle n'est tenté.
- `rpc_client.py` conserve les retries/coalescings/fallbacks uniquement pour les lectures explicitement allowlistées. `sendTransaction` est refusé par `rpc_post`; `submit_transaction` tente une seule soumission primaire et l'issue ambiguë reste `UNKNOWN` pour réconciliation read-only.
- Les mesures incluent réception durable (`event.inbox_enqueue`), parse, décision, traitement et latence de bout en bout avec le même ID opaque. Les métriques n'incluent pas signatures, secrets, wallets, mints ni corps de réponse. Les compteurs de refus, doublons et replays sont des taux d'observation synthétiques; ils ne mesurent pas la perte finale réelle.
- La couverture sécurité héritée de PR #11 comprend la vérification allowlist Telegram dans `on_button` et la réduction au silence des légendes/contenus d'image dans `Notifier.notify_photo`.

## Matrice de preuves

| Exigence | Code et preuve | Résultat / limite |
|---|---|---|
| HTTP 429 Enhanced ne perd pas une notification déjà reçue | Inbox avant parse; test synthétique de retry NewToken et Protection | Rejeu local vérifié; aucun endpoint Helius réel appelé |
| Redémarrage et crash | SQLite persistant, bail et marqueur d'effet; tests de migration v2, reprise avant effet, blocage `UNKNOWN` après effet | Scénarios injectés hors ligne; aucune garantie exactly-once |
| Saturation/backpressure | Bornes count/bytes, pause et rattrapage; test de queue pleine | Test isolé SQLite; aucun volume réel mesuré |
| Checkpoint et replay | Avancement contigu après traitement; historique lu en ordre ancien→récent; test checkpoint absent de l'historique et gap sans checkpoint | Une récupération sans checkpoint d'un flux déjà actif échoue maintenant fermé; pagination fournisseur, rétention, forks/commitments et quotas réels non vérifiés |
| Concurrence/dédoublonnage | `BEGIN IMMEDIATE`, contrainte `(stream, signature)`; test de deux claimants et doublon après redémarrage | Le tombstone persistant croît dans le temps |
| RPC de transaction | Refus via `rpc_post`; tests simulés 429, timeout et signature retournée | Aucun appel RPC de soumission réel |
| Callback Telegram | Tests hérités de PR #11: autorisé, refusé, allowlist absente/mal formée, callback invalide | Validation locale hors ligne; aucun bot Telegram réel |
| Confidentialité `notify_photo` | Tests hérités de PR #11 sur légende, contenu binaire et erreur | Tests de logs locaux; aucun envoi Telegram réel |
| Corrélation/latence | Test synthétique parse→décision→fin avec ID commun, absence de signature dans le fichier métrique, p50/p95/p99 | Échantillon synthétique; les percentiles ne décrivent pas la production |
| Skills PR #9 | La PR indique N01 seul observé négativement; P01/A03 routage seulement, P02 échec d'environnement, les autres `NOT_RUN` | Validation comportementale incomplète; la branche Skills reste séparée |

Les tests synthétiques valident uniquement les chemins exécutés. Les callbacks y sont des doubles locaux, et les signatures utilisées sont des marqueurs de test, pas des transactions signées ou soumises.

## Limites et contrôles opérationnels ouverts

### Stockage et sémantique de livraison

- La file d'attente et les payloads non traités sont bornés, mais le fichier SQLite total ne l'est pas : les signatures tombstone nécessaires à la déduplication durable restent conservées. Prévoir supervision disque et rotation/archivage opérés après revue; une rétention bornée affaiblirait la déduplication à long terme.
- L'inbox réside sur un disque local. La durabilité après panne matérielle, restauration d'une sauvegarde dans l'environnement cible, partage multi-processus/multi-hôte et quotas réels du fournisseur n'ont pas été validés.
- Quand aucun checkpoint n'existe, aucun historique ne peut être borné à un point antérieur connu; le bootstrap vide n'est pas une preuve d'absence d'événements manqués avant le premier événement confirmé.
- Si un flux déjà démarré se déconnecte avant son premier checkpoint, la reprise reste bloquée `missing_checkpoint`; aucun outil de bootstrap/reset opérateur n'est ajouté dans cette mission. Ce blocage évite une reprise silencieuse, mais demande une procédure opérationnelle avant déploiement.
- Un effet effectué puis interrompu avant l'ack ne peut pas être distingué d'un effet non effectué. Le code choisit `UNKNOWN` et revue manuelle. Exactly-once n'est ni implémenté ni revendiqué; le comportement dépend aussi de l'idempotence des callbacks aval.
- `getSignaturesForAddress` est supposé fournir une histoire exploitable pour les flux programmés. Sa rétention, la relation avec les notifications WS `processed`, la progression des slots et la sémantique de chaque programme DEX doivent être vérifiées dans l'environnement cible. Un doute bloque la reprise.
- Les taux `unpersisted_rejection_rate`, `duplicate_observation_rate` et `replay_observation_rate` reflètent les compteurs observés depuis la création de l'inbox; ils ne révèlent pas les événements inconnus du bot ni ne prouvent un taux de perte de zéro.

### Missions 7 et 8 non clôturées

- Mission 7 / PR #8 reste ouverte et non fusionnée. Aucun soak PAPER de 24 h n'a été exécuté; la restauration opérationnelle, les quotas Helius réels et l'audit indépendant de la sémantique, du code déployé et des autorités d'upgrade DEX restent à faire. LIVE reste bloqué.
- Mission 8 / PR #10 reste ouverte et non fusionnée; PR #11 est une branche sœur basée sur PR #8. La PR #10 mesure un échantillon RPC public court, pas un soak bot ni une validation de région cible; le taux réel de perte WS et les percentiles de charge de production restent inconnus.
- PR #9 reste une branche Skills distincte. Son propre rapport ne valide pas les cas marqués `NOT_RUN`; aucune branche bot ne prétend avoir validé le routage des Skills.
- `web-research-problem-solver` a été vérifié dans le catalogue local BOT MT5, où il est limité aux missions MT5; son manifeste n'est pas dans le dépôt Solana. Il n'a pas été invoqué car aucun blocage technique du code n'a nécessité une recherche sourcée.

## Validation de cette copie

- Tests ciblés Mission 8.5/RPC/métriques : `49 passed`.
- Suite offline complète : `525 passed` avec `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `PYTHON_DOTENV_DISABLED=true`, PAPER forcé, LIVE désactivé, variables de clés/tokens vides et RPC configuré sur `127.0.0.1:9`.
- `python -m compileall -q .` : réussi.
- `git diff --check` : réussi; aucun fichier non fusionné n'était signalé.
- Revue indépendante finale : le premier passage a trouvé la reprise permissive d'un flux actif sans checkpoint; elle a été corrigée en `missing_checkpoint` fail-closed, couverte par un test, puis revue de nouveau sans autre défaut P0/P1 relevé.
- CI GitHub [45385950171](https://github.com/cheicki225/-meme-coin/actions/runs/45385950171), sur le head code `5294d24f6a2d68ecf48f1921c804b4187fe0a949` : réussi. La CI a confirmé 525 tests, compilation, défaut PAPER, Ruff (`E9,F63,F7,F82`), pip-audit (`No known vulnerabilities found`) et Gitleaks (`no leaks found`). Couverture combinée ligne+branche : **34,31 %**; artefact [`mission-coverage-45385950171`](https://github.com/cheicki225/-meme-coin/actions/runs/45385950171/artifacts/14915798300). Gitleaks a aussi déposé le SARIF `14916681090` sans constat bloquant.
- Ruff, pip-audit et Gitleaks n'ont pas été exécutés localement. `ruff`, `coverage` et `pip-audit` ne sont pas installés; l'installation temporaire locale a échoué sur la validation TLS de PyPI, sans désactivation TLS. La CI distante a exécuté et réussi ces contrôles.
- Aucun RPC externe, WebSocket Helius, wallet, clé privée ou `.env` n'a été consulté; aucune signature ou soumission réelle, aucun ordre et aucune activation LIVE. Les marqueurs `signature-*` sont des chaînes synthétiques uniquement.

## Retour arrière

La branche est isolée. Pour abandonner les changements de Mission 8.5 sans toucher aux branches existantes, fermer la PR de suivi éventuelle et supprimer uniquement cette branche/clône après avoir conservé les rapports et les commits utiles. Ne pas supprimer le fichier SQLite de l'environnement opérationnel sans procédure de sauvegarde et de reprise.
