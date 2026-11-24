# Mission 11 — Unified PAPER Bot & Final Integration

**Statut : `PARTIALLY_READY / MANUAL_VERIFICATION_REQUIRED`**

**Branche de travail :** `codex/mission-11-unified-paper-20261124`

**Base de la future PR :** PR #21, `c2b982c44034e4f37dd887fdabc3a28ff486e2d0`

**Tête locale validée :** `c17138c`

**Date :** 24 novembre 2026

Mission 11 a été exécutée dans un clone neuf sous `%LOCALAPPDATA%`. Le checkout MT5 et le clone Solana `main` déjà modifiés ont été laissés intacts. Aucune branche source #19, #20 ou #21 n’a été modifiée, et aucune PR n’a été fusionnée.

## 11A — Graphe et intégration sélective

Les refs actuelles vérifiées sont : PR #19 `26d19c552a2b6a3ed5e307fc256ed1466f5ae9c5`, PR #20 `9b024c11347e3c96436ec6d7a27630d866012ee7`, PR #21 `c2b982c44034e4f37dd887fdabc3a28ff486e2d0`, toutes trois basées sur le SHA #12 `5b6b155919654182dab25ae14f98c5bf61df023a`. #21 contient déjà la pile Mission 10 sélectionnée depuis #12.

```text
                       ┌─ #19 source 26d19c5 ── cherry-pick 1472fd2 ─┐
#12 shared base 5b6b155 ├─ #20 source 9b024c1  ── cherry-pick 07f27ee ─┼─ on #21
                       └─ #21 Mission 10 c2b982c ─────────────────────┴─> c17138c
```

Les commits sources ont été repris avec `git cherry-pick -x` : #19 comme `1472fd2`, puis #20 comme `07f27ee`. Les deux reprises se sont appliquées sans conflit. #19 ajoute la comptabilité de flux confirmés, sa valorisation et son test ; #20 protège les ajouts PAPER « buy the dip » avec le même snapshot de taux et de frais que celui utilisé pour la mutation. Les chemins #10/#11 déjà contenus dans #21 n’ont pas été rejoués, et l’historique complet recouvrant de #13 n’a pas été fusionné.

| Source | Fichiers du patch source | Résultat de comparaison avec #21 |
| --- | --- | --- |
| #19 | `monitoring_list.py`, `transaction_accounting.py`, `tests/test_transaction_accounting.py` | Aucun de ces fichiers n’était modifié par le delta Mission 10 #21 ; reprise propre. |
| #20 | `paper_trader.py`, `tests/test_paper_trader_cashflow.py` | Le delta #21 ne modifiait pas ces fichiers ; reprise propre. |

La PR #19 conserve `accounting_domain=CONFIRMED_CHAIN` et `applied_to_paper_pnl=false`. Ses nouveaux champs sont séparés des positions, totaux et compteurs PAPER. Les runs exacts des têtes sources vérifiés sur GitHub sont verts : [CI #19, run 45447282273](https://github.com/cheicki225/-meme-coin/actions/runs/45447282273), [CI #20, run 45485900713](https://github.com/cheicki225/-meme-coin/actions/runs/45485900713) et [CI #21, run 45488806763](https://github.com/cheicki225/-meme-coin/actions/runs/45488806763).

**Limite d’idempotence à résoudre avant de considérer le ledger complet :** #19 ne conserve que 1 000 enregistrements. La déduplication recherche la signature dans cette fenêtre; une signature supprimée par la rétention peut être réinsérée lors d’un rejeu tardif. Les tests valident un doublon encore retenu, pas le rejeu après éviction. Le ledger n’a pas de callsite de production dans cette tête, donc il n’est pas raccordé à un traitement PAPER ou LIVE. La fenêtre est notée ici explicitement ; une clé de déduplication durable doit être décidée avant un raccord runtime.

## 11B — Pipeline PAPER et provenance SOL/USD

Les parcours déjà raccordés sont distincts : le listener de nouveau token appelle `SniperBot.on_new_token`, qui peut atteindre `PaperTrader.open_position`; le callback copy-trade a ses contrôles propres avant l’ouverture. Le collecteur de wallets tourne en service parallèle et stocke des candidats/incertitudes dans sa base de collecte. Ses événements ne sont pas branchés au scoring ni aux entrées PAPER. Aucun nouveau raccord n’a été inventé à partir de données candidates.

Le taux SOL/USD utilisé par le chemin PAPER demeure un `float` fourni par `backtest.get_sol_usd_rate()`. Le helper peut reprendre le dernier taux même périmé ou la constante configurée sans propager une source ni un âge au trade. Le nouveau helper #19 exige `rate_verified`, `rate_source` et un timestamp, mais ces champs restent une attestation de l’appelant ; ils n’authentifient pas une réponse externe. En conséquence, ni la valorisation #19 ni le PnL/exposition PAPER existants ne sont présentés comme ayant une provenance externe vérifiée. Le raccord de cette provenance au pipeline PAPER reste ouvert.

Les tests #19 couvrent des deltas/frais mal formés, taux manquants, futurs ou périmés, valorisation inconnue puis réconciliée, idempotence dans la fenêtre et rollback de sauvegarde. Il n’existe pas de test de bout en bout collecteur → décision → admission → simulation → positions → PnL.

## 11C — Entrées, filtres et sorties

La revue confirme que sniping, copy trading et anti-rug sont des capacités distinctes, et non une preuve d’intégration de bout en bout. La classification copy-trade reste heuristique pour les transferts SOL/token dans une même transaction ; elle ne décode pas toutes les routes ou CPI. Le scanner rug enregistre, filtre et notifie, mais n’appelle pas directement `open_position`. Les filtres anti-rug GoPlus sont facultatifs par configuration et restent heuristiques, sans prouver la possibilité de vente.

La sortie à durée maximale n’utilise plus `entry_price` quand le prix de sortie est manquant, nul, négatif, non fini ou mal formé. La position et son coût de base restent ouverts; l’état indique `exit_status=unknown`, `exit_pending_reason=exit_price_unavailable`, et aucune perte ni aucun gain n’est réalisé. Les ventes Telegram rapides, partielles, totales, « sell initials » et le callback copy-sell n’utilisent plus le prix d’entrée comme valeur de remplacement. L’interface des positions signale aussi l’état inconnu.

Au démarrage PAPER, les positions persistées avec un `entry_time` valide reprennent leur moniteur avec la durée restante. Si la date est absente, invalide ou future, l’âge ne peut pas être reconstruit : la position reste ouverte, marquée `unknown`, et le moniteur automatique ne redémarre pas. La vue Telegram demande alors une vérification manuelle. Cette politique évite de redémarrer artificiellement la durée, mais ne restaure pas automatiquement les sorties pour ces enregistrements historiques incomplets.

Les tests ajoutés couvrent expiration avec prix absent/nul/négatif/non fini/mal formé ou valide, échec de sauvegarde de l’état inconnu, reprise PAPER et déduplication de tâches de moniteur, `entry_time` absent/invalide/futur, prix manquant dans une vente Telegram, ainsi que limites d’âge copy-trade absentes ou invalides. Le seuil d’âge configuré lui-même n’a pas été modifié.

## 11D — Telegram, monitoring et confidentialité

Les commandes `/stats`, `/positions`, `/performance` et `/reconcile` existaient dans #21 et restent la surface d’observation disponible. Elles affichent PnL agrégé, état d’arrêt, positions, métriques Helius localisées dans l’inbox et compteurs/latences RPC locaux. Ces métriques ne prouvent pas la disponibilité du fournisseur. L’arrêt d’urgence initial reste actif par défaut; les callbacks restent vérifiés et fail-closed. Aucun changement n’a été fait aux chemins d’envoi RPC de `rpc_client.py` ou de `jupiter_executor.py`.

Les signatures complètes sont maintenant tronquées dans le refus Telegram de `/resume` et les notifications de récupération au démarrage. Aucun tableau de bord Web ou framework d’interface n’a été trouvé dans l’arbre ; aucun dashboard n’a été ajouté.

## 11E — Délégation, contrôles et limites

Trois revues de domaine read-only ont été réalisées (comptabilité/provenance, sniping-copy/anti-rug-sorties, Telegram/monitoring), suivies d’une revue sécurité indépendante du diff. Elle n’a pas trouvé de régression P0/P1 dans les changements. Elle a signalé la fenêtre d’idempotence du ledger, les limites de provenance ci-dessus et les signatures Telegram complètes, ces dernières étant maintenant tronquées.

Le Skill `web-research-problem-solver` n’est pas présent dans l’arbre de skills de la tête #21 (`SKILL_UNAVAILABLE`). Aucun blocage technique non résolu nécessitant une source externe n’a rendu son workflow de recherche nécessaire dans cette session. Les PR source et GitHub ont été consultés pour vérifier les refs et runs, sans accès à un wallet, clé ou secret.

### Validation effectivement exécutée

Environnement local : Python 3.11.9 ; `PYTHON_DOTENV_DISABLED=true`; chargement automatique des plugins pytest désactivé selon `TESTING.md` pour cette machine.

- `python -m compileall -q .` — réussi.
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q` — **687 passed**.
- `git diff --check` — réussi avant commit d’intégration ; les validations ci-dessus ont été exécutées sur la tête locale `c17138c`.
- Ruff, `pip-audit`, Coverage.py et Gitleaks ne sont pas installés localement. Le workflow GitHub de la nouvelle PR doit encore exécuter Ruff, pip-audit, Gitleaks, pytest avec couverture lignes/branches et compilation sur la tête exacte de Mission 11.

### Sécurité et éléments non vérifiés

Aucune clé, wallet, `.env`, secret ou URL RPC avec identifiant n’a été lu. Aucun RPC n’a été soumis, aucune transaction réelle n’a été signée/envoyée, aucun Telegram déployé n’a été contacté, aucun LIVE ni mainnet n’a été utilisé. Aucun soak PAPER réel de 24 h, drill de restauration de l’environnement cible ou audit DEX/CPI n’est revendiqué. La revue et les tests sont hors ligne/statique/mimés.

## Prochaines étapes priorisées

1. Obtenir les résultats CI complets sur la tête exacte de la PR Mission 11 et corriger les échecs dans cette branche uniquement.
2. Choisir une déduplication durable pour les signatures comptables après la fenêtre de 1 000, avant d’ajouter un callsite runtime.
3. Introduire une preuve de provenance SOL/USD produite par un adaptateur fournisseur (source, timestamp observé, âge et statut) et traiter explicitement les caches périmés ; ne pas réutiliser `rate_verified` caller-attested comme preuve.
4. Ajouter une matrice d’intégration offline couvrant les interfaces qui restent séparées. Garder la collecte candidate hors du scoring tant qu’une règle de provenance/qualité et une interface n’existent pas.
5. Les soak 24 h, restoration drill cible et audit DEX/CPI restent des gates d’environnement et d’expertise distinctes ; ils ne sont pas simulés ici.
