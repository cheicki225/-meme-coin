# Mission 8.6 — Integration & Regression Validation

Date : 24 novembre 2026. Statut : **PARTIALLY_READY / MANUAL_VERIFICATION_REQUIRED**. LIVE : **BLOCKED**.

## Périmètre et heads vérifiés

La branche locale `codex/mission-8.6-integration-2026-11-24` part du head exact de la PR #10 et incorpore la PR #11 par un merge commit. Le checkout `main`, les branches sources et leurs worktrees n’ont pas été modifiés.

| Élément | Branche / head observé | Relation |
|---|---|---|
| PR #8 — Mission 7 | `codex/mission-7-reliability-2026-11-22` — `b8438734b00c18a265f0907e83cb5e7bff25a43f` | Ouverte, non fusionnée; base de #10 et #11 |
| PR #10 — Mission 8 performance | `codex/mission-8-performance-2026-11-22` — `2007b6618d426ce9edd3adb501a598d2022a8a61` | Ouverte, non fusionnée; base de la branche d’intégration |
| PR #11 — corrections sécurité | `codex/security-fixes-current-m7-2026-11-24` — `2d0df1e9014e568e4fd4efd83308358a0e505721` | Ouverte, non fusionnée; branche sœur de #10 sur #8 |
| Merge local Mission 8.6 | `0993ca7f72b00755dc8a52e64a3f985655034e2f` | Parents : head #10 puis head #11 |

Les PR #8, #10 et #11 étaient toutes ouvertes et non fusionnées lors de l’inspection. La PR d’intégration dédiée, si créée, doit cibler la branche #10; aucune fusion automatique n’est demandée.

## Intégration et conflits

La fusion a révélé deux conflits de contenu : `rpc_client.py` et `tests/test_rpc_retry_policy.py`.

- `rpc_client.py` conserve le limiteur par hôte, l’allowlist de lectures, les retries et le coalescing bornés aux lectures idempotentes de #10. L’entrée générique refuse `sendTransaction`; `submit_transaction` effectue une seule tentative primaire et produit `UNKNOWN` sans retransmission ni secours automatique. Les requêtes de réconciliation restent des lectures.
- Les tests RPC de #10 ont été conservés. Le test historique qui envoyait `sendTransaction` par `rpc_post` a été adapté à l’API spécialisée de #11 et vérifie une seule tentative primaire, sans fallback. Des régressions 429, timeout et succès attendus sont incluses.
- Les corrections Telegram de #11 restent présentes : allowlist privée fail-closed sur les callbacks avant parsing ou effet de bord, et logs `notify_photo` expurgés.
- À la revue finale, le défaut d’un nouvel état a été rendu fail-closed : `emergency_stop=True` avec motif `startup safety default`. Un état existant sans cette clé est backfillé comme arrêté. Un état qui porte explicitement `False` reste respecté, car il représente un déverrouillage persistant de l’opérateur.

Les deux têtes sources demeurent inchangées. Le merge commit local est construit sur #10 et a pour second parent le head #11.

## Validations exécutées hors ligne

Interpréteur local : Python 3.11.9. `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` et `PYTHON_DOTENV_DISABLED=true` ont été utilisés; le processus de test a reçu PAPER, LIVE désactivé et les variables de credentials/RPC vides. Aucun bot n’a démarré.

- Matrice ciblée : **57 tests réussis** couvrant les réponses 429, timeout, concurrence/coalescing, récupération et persistance SQLite, reconnexion WebSocket, callbacks Telegram et non-divulgation dans les logs.
- Après activation du stop par défaut : **21 tests ciblés réussis** sur récupération SQLite/JSON, stop d’urgence et contrôles de risque. Ils vérifient nouvel état arrêté, backfill des états persistés sans champ, préservation d’un état explicitement déverrouillé et reprise après récupération.
- Suite complète après le changement : `python -m pytest -q` — **510 tests réussis**. Elle reprend toute la matrice RPC, WS, Telegram, logs et persistance.
- Compilation : `python -m compileall -q .` — réussie.
- `git diff --check` — réussi après les modifications finales.
- `coverage` et `ruff` ne sont pas installés dans l’interpréteur local (`No module named coverage` / `No module named ruff`). Aucun pourcentage de couverture ni résultat Ruff local n’est revendiqué; les résultats CI sont consignés ci-dessous.

PAPER est le défaut du code; LIVE est désactivé dans l’environnement de test. Les appels réseau des tests sont simulés. Aucune requête RPC externe, signature réelle, transaction signée ou envoyée, wallet ni secret n’a été utilisé.

## CI GitHub

La PR d’intégration [#12](https://github.com/cheicki225/-meme-coin/pull/12) cible la branche #10 et reste ouverte/non fusionnée. Le run final du code [45379533708](https://github.com/cheicki225/-meme-coin/actions/runs/45379533708), sur le head `d0dd806fefb9962a12eb23e2123d8afea8ce5cd1`, est **vert** : compilation, défaut PAPER, **510 tests**, Ruff, pip-audit (« No known vulnerabilities found »), Gitleaks (« no leaks found ») et upload de l’artefact couverture ont réussi.

La couverture CI est de **32,44 % combinée** (10 387 statements et 3 818 branches mesurés) selon `coverage report`; le JSON/XML sont dans l’[artefact `mission-coverage-45379533708`](https://github.com/cheicki225/-meme-coin/actions/runs/45379533708/artifacts/14913565335). Le workflow a émis un avertissement non bloquant : `actions/upload-artifact@v4` vise Node.js 20 et est forcée sur Node.js 24 par le runner.

## Revue indépendante

Trois revues statiques indépendantes ont confirmé les invariants RPC, Telegram/logs et la résolution du merge. Après le changement de stop par défaut, une quatrième revue en lecture seule a confirmé le fail-closed des états neufs et sans champ historique, ainsi que la préservation d’un `False` explicitement persisté. Aucun bot, RPC externe ni transaction n’a été utilisé.

## Risques et limites encore ouverts

- Le parsing Helius Enhanced peut encore répondre `{}` après HTTP 429. Dans le listener de création de tokens, un résultat vide fait abandonner l’événement reçu; cette branche ne fournit pas d’inbox durable ni de rattrapage pour ce chemin. La PR #10 avait déjà documenté ce risque. La Mission 8.5 n’a pas de PR publique identifiée lors de cette vérification; son avancement non publié est suivi dans un autre fil et n’est pas dupliqué dans ce worktree. Ne pas considérer le risque 429/perte de signature comme fermé.
- Le stop d’urgence est désormais actif par défaut pour un nouvel état et pour une base ancienne sans cette clé; un `False` déjà persisté reste explicite et n’est pas écrasé. Un nouvel état PAPER reste donc bloqué jusqu’au `/resume` d’un utilisateur autorisé. Aucun bot ni base opérationnelle n’a été démarré, consulté ou modifié, donc l’état d’arrêt d’urgence opérationnel n’est pas attesté.
- Aucun soak PAPER de 24 h, audit sémantique des programmes DEX/CPI, test complet de chargement des Skills ou vérification Demo n’a été exécuté.
- Le Skill de recherche web externe n’est pas livré dans le catalogue du dépôt (`SKILL_UNAVAILABLE`). Aucun blocage technique non résolu n’a requis une recherche sourcée; aucune recherche web de dépannage n’a été simulée.
- Le run CI vert couvre le head indiqué ci-dessus; il n’exécute pas de vérification Demo ni de test d’exploitation.

## Suite

La PR #12 est publiée et la CI du code final est verte. Le commit suivant ne met à jour que ce rapport. Garder le risque de perte d’événement Helius 429 visible en revue; ne fusionner aucune PR et ne pas revendiquer de validation LIVE.
