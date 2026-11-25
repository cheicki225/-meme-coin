# Mission 19 — Data Integrity, Runtime Integration & Recovery

**Statut : `PARTIALLY_READY / MANUAL_VERIFICATION_REQUIRED`**  
**Date : 2026-11-25**

Mission 19 renforce la reprise de l’inbox Helius avec une fenêtre de reconnexion bornée, des ordres de transaction rapportés par RPC et des générations de reprise vérifiées à chaque frontière de claim. Elle ajoute un contrat hors ligne d’observation SOL/USD qui garde explicitement la quote non vérifiée. L’admission PAPER, les positions et le PnL ne sont pas raccordés transactionnellement au runtime : cette exigence reste bloquée.

## Base, périmètre et isolation

- Dépôt : `cheicki225/-meme-coin`.
- Base vérifiée : PR #30, `codex/mission-18-core-paper-integration-20261125`, head `e9334d5716b7fd5969cae3f077bb9fc1a2a1d1d7`.
- Worktree isolé : `C:\Users\LENOVO\AppData\Local\BOT-MEME-COIN-MISSION19-20261125`, branche `codex/mission-19-data-integrity-20261125`.
- Le checkout utilisateur et le worktree Mission 18 sont restés intacts. La Mission 19 n’a pas été fusionnée dans `main`.
- PR #30 demeure une dépendance brouillon/non fusionnée; sa CI `45701063429` est la preuve du head M18 exact, pas de la Mission 19. La PR brouillon #33 a été ouverte le 25 novembre 2026 vers cette base, à partir du head initial M19 `cf67581e30d4641ee17c77531c2bff3fb7d6214a`; aucun résultat CI M19 exact-head n’était disponible à l’ouverture.
- Aucune valeur de `.env`, clé, wallet, signature, compte, processus ou base cible n’a été lue. Les tests ont reçu explicitement `TRADING_MODE=PAPER`, `LIVE_EXECUTION_ENABLED=false` et `PYTHON_DOTENV_DISABLED=true`.

## Changements réalisés

### Inbox Helius et récupération

- Le schéma passe à la version 7. Il persiste la génération de reprise sur le flux et sur chaque claim, avec une borne de reconnexion durable.
- Un nouveau signal de trou, un UNKNOWN, une queue pleine, un effet dont le bail expire ou un ACK qui chevauche une reprise incrémente l’époque, annule l’ancienne borne et garde l’intake en pause.
- Avant d’utiliser une souscription, le listener attend les ACK JSON-RPC initiaux. Les notifications intercalées pendant la négociation sont conservées dans un tampon borné à 1 000 trames. Un ACK doit avoir `jsonrpc="2.0"`, un ID exact de type entier, aucun champ `error` et un résultat d’abonnement de type entier; les réponses malformées ne consomment pas le pending ID.
- Après les ACK, la reprise capture et persiste le slot global `processed`. Elle garde la pause jusqu’à ce que le slot global `finalized` atteigne cette borne. Les erreurs de finalité ne sont pas converties en succès.
- Ensuite, le replay finalisé de `getSignaturesForAddress` doit paginer jusqu’au checkpoint durable, contrôler l’ordre décroissant strict `(slot, transactionIndex)`, refuser les doublons et toute contradiction, et rester dans la limite configurée. Une adresse inactive n’a pas besoin d’avoir une transaction dans le dernier slot global; l’absence d’événement est alors établie par l’historique complet rapporté, sans prétendre le vérifier indépendamment. Trois erreurs consécutives du RPC finalized rendent la récupération incomplète pour que le listener reconnecte avec le flux toujours bloqué; un simple retard de finalité continue d’attendre.
- Un indicateur d’arrêt est vérifié pendant l’attente, avant le replay et avant chaque claim de tous les drains de reprise, d’ingestion et de retry. Un événement déjà en cours d’exécution peut finir; aucun claim suivant ne démarre après l’arrêt observé. Les listeners arrêtés ne traitent pas les notifications encore tamponnées; `ProtectionScanner.stop()` ferme également le WebSocket actif sans changer sa signature synchrone.
- Les claims de reprise gardent l’époque qui les a émis. Le démarrage d’effet, le retry et l’ACK vérifient que l’époque est toujours actuelle; un ACK concurrent avec une nouvelle reprise devient `UNKNOWN` et rebloque le flux.
- Le code refuse maintenant une base SQLite de version future avant de changer son mode journal ou d’exécuter du DDL. Un test confirme qu’une base future reste en mode `DELETE`, garde son `user_version` et ne reçoit aucune table.
- Un événement `UNKNOWN` dû uniquement à l’absence initiale d’index peut être enrichi par le replay finalisé si aucun effet n’a commencé et si l’identité/order ne contredit pas l’état durable. Les autres causes d’UNKNOWN restent bloquantes.

La finalité, le slot et `transactionIndex` sont toujours des valeurs rapportées par le RPC configuré. Le protocole n’établit ni intégrité indépendante du fournisseur, ni preuve cryptographique de complétude, ni protection indépendante contre un historique élagué ou incorrect. Si le champ d’ordre n’est pas fourni, si le checkpoint manque, si l’historique ne peut être paginé complètement ou si une contradiction apparaît, la récupération reste bloquée. Les nœuds Agave antérieurs à v4.0 peuvent ne pas renvoyer `transactionIndex` pour cette méthode. Voir la documentation officielle [Solana `getSlot`](https://solana.com/developers/docs/rpc/http/getslot) et [Solana `getSignaturesForAddress`](https://solana.com/developers/docs/rpc/http/getsignaturesforaddress), consultée le 25 novembre 2026.

### Observation SOL/USD

`sol_usd_provenance.py` ajoute un adaptateur Pyth Hermes injecté et hors ligne. Il contrôle le flux configuré, les champs numériques, la charge binaire, l’heure `publish_time`, l’âge et la confiance avec des valeurs `Decimal`.

Le module n’ouvre pas de transport réseau, ne choisit ni clé ni flux officiel, et ne vérifie pas cryptographiquement la charge signée. Le résultat indique `feed_mapping_status=caller_configured_unverified`, `trust_status=provider_reported_unverified`, `signature_verified=False` et `accounting_eligible=False`. Les fixtures n’emploient pas de vraie identité de flux. Le taux SOL/USD, les frais, positions et valorisation restent bloqués. Le schéma Hermes expose notamment le prix, l’intervalle de confiance, `publish_time` et une charge binaire; leur présence dans une fixture ne prouve pas la source ou la signature ([Pyth Hermes Latest Price Updates](https://docs.pyth.network/api-reference/hermes/latest-price-updates), [vérification Pyth sur Solana](https://docs.pyth.network/pro/use-prices-on-blockchain/solana), [sémantique de l’intervalle de confiance](https://docs.pyth.network/pro/understanding-price-data)).

### Intégration de l’admission et du PnL

Les primitives SQLite de Mission 18 restent hors du chemin d’admission exécuté par `main.py`/`PaperTrader`. L’identité de certains signaux dépend encore d’un timestamp local non stable au replay, et les réservations d’exposition, l’admission, la position et le PnL ne partagent pas une transaction durable commune. Les raccorder dans cet état ne préserverait pas les invariants d’idempotence et d’exposition. Aucun code de trading n’a été ajouté et aucun effet PAPER n’est prétendu opérationnel.

## Résultats des critères

| Critère | Résultat | État |
|---|---|---|
| UNKNOWN d’ordre manquant sans effet | Enrichissement conditionnel par historique finalisé; autres UNKNOWN et ambiguïtés restent bloquants. | Partiel, confiance RPC seule |
| Récupération concurrente et reprise crash-safe | ACK, watermark, finalité, replay canonique et fencing d’époque testés avec transports simulés. | Validé hors ligne pour les cas testés |
| Source SOL/USD attestable | Parseur strict avec temps fournisseur, âge et confiance; mapping et signature non vérifiés. | Bloqué pour comptabilité |
| Admission PAPER, exposition, position et PnL atomiques | Aucun call site transactionnel runtime prouvé. | Bloqué |
| Soak/restauration M20 | Aucun environnement cible ni autorisation de run; aucun soak ou restore cible exécuté. | Manuel/bloqué |
| DEX/CPI sémantique | Tests connus à base de fixtures; aucune revue indépendante déployée. | Non audité |

## Vérifications effectuées

Python 3.11.9, Windows. Commandes de test exécutées avec les variables PAPER ci-dessus et `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`.

| Vérification | Résultat |
|---|---|
| Sous-suite Helius, replay, compatibilité Mission 12, readiness et provenance | `95 passed` en 5,65 s. |
| Suite complète | `887 passed` en 24,72 s. |
| `python -m compileall -q .` | Réussi. |
| `git diff --check` | Réussi. |
| Mypy ciblé avec `--follow-imports=silent` sur cinq modules | Trois diagnostics existants, déjà présents sur le head M18 : `protection_scanner.py:402`, `protection_scanner.py:483`, `copytrade_listener.py:630`. Aucun nouveau diagnostic dans le store ou l’adaptateur prix. |
| Ruff, pip-audit, Gitleaks et coverage locaux | Binaires/modules absents; aucune dépendance installée. Ils doivent être vérifiés par CI sur le SHA exact. |
| Tests avec autoload pytest par défaut | Le chargement du plugin global Web3 échoue avant la collecte car `web3.tools.pytest_ethereum` importe un symbole `ContractName` absent de l’installation `eth_typing`. Le désactivation des plugins tiers via `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` a permis l’exécution complète sans changer l’environnement installé. |
| Fournisseurs, RPC, réseau d’exécution et target PAPER | Aucun appel réseau ou démarrage. Aucune clé, signature, transaction, ordre, dépense, déploiement ou merge. |

Trois sous-agents Codex distincts ont fourni des audits en lecture seule : Helius/reprise, provenance prix/runtime et gates Missions 20–22. Les revues Helius ont signalé des défauts P1 de borne/fencing, d’arrêt pendant la reprise et de drain après arrêt; les cas ont été corrigés puis testés. Les findings P2 sur l’ACK malformé et les échecs répétés du RPC finalized sont également corrigés et couverts. La revue finale Helius confirme qu’il ne reste aucun défaut P1/P2 dans le périmètre examiné. La PR brouillon #33 est ouverte et empilée sur #30; son head évolue avec les mises à jour du rapport. Le statut de CI doit être vérifié sur le SHA courant de la PR avant toute conclusion de validation.

## Gates ouvertes et risques résiduels

1. **P1 — Confiance du replay :** l’exhaustivité et l’ordre viennent du fournisseur RPC configuré; aucun second fournisseur ni preuve cryptographique n’est utilisé.
2. **P1 — USD comptable :** pas de mapping de feed revu, de vérification de signature ou de fournisseur contacté.
3. **P1 — Admission durable :** signal, identifiant, exposition, admission, position, frais et PnL ne partagent pas de commit atomique runtime.
4. **P1 — Historique ancien :** les préfixes traités avant l’ordre canonique peuvent nécessiter une réconciliation opérateur; aucun état cible n’a été inspecté.
5. **P1 — Opération :** pas de soak de 24 h, d’exercice de restauration cible ou de quota fournisseur observé.
6. **P1 — DEX/CPI :** aucune revue sémantique indépendante à partir de sources versionnées.
7. **P2 — Dépendances CI :** couverture, Ruff, audit de dépendances et secret scan locaux ne sont pas disponibles dans cet environnement; seule une CI sur le SHA exact peut établir leur résultat.

**Conclusion :** l’inbox Helius est plus prudente pour certains cas UNKNOWN et la fenêtre de reprise; le nouvel adaptateur rend visibles les limites de confiance d’une quote. Le bot ne possède toujours pas d’admission PAPER runtime et d’accounting SOL/USD vérifiés. Les Missions 20 et 22 restent `MANUAL_VERIFICATION_REQUIRED`; aucun readiness ou rendement live n’est revendiqué.
