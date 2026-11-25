# Mission 14 — PnL, backtesting et optimisation

**Statut courant : `PARTIALLY_READY / MANUAL_VERIFICATION_REQUIRED`**

**Branche :** `codex/mission-14-pnl-backtesting-20261124`

**Base vérifiée :** PR #22, head `9590196c40a64ef478a4d4e7c44ade5dee105f12`

**PR Mission 14 :** [#25 — Mission 14: harden PnL ledger and chronological backtesting](https://github.com/cheicki225/-meme-coin/pull/25), brouillon ouvert, non fusionné.
**PR Mission 14 :** à renseigner après publication de la branche.

## Périmètre et dépendances

- PR #19 est ouverte, non fusionnée, head `26d19c552a2b6a3ed5e307fc256ed1466f5ae9c5`. Ses primitives comptables avaient une fenêtre d'idempotence limitée à 1 000 lignes et acceptaient une attestation `rate_verified` fournie par l'appelant.
- PR #21 est ouverte, en brouillon, head `0cc54f77312d6fbf89e0f99c191f840bbddaf985`.
- PR #22 est ouverte, en brouillon, mergeable, et cible exactement le head courant de #21. Elle reprend déjà les changements #19 et #20. La Mission 14 est empilée sur son head exact `9590196c40a64ef478a4d4e7c44ade5dee105f12`.
- PR #23 est ouverte, en brouillon, mergeable, et reste une branche sœur basée sur #21 au SHA `0cc54f77312d6fbf89e0f99c191f840bbddaf985`. Elle modifie `helius_event_store.py`, `docs/MISSION_12_CAPABILITY_MATRIX.md` et `tests/test_mission12_offline_pipeline.py`; ces changements ne sont pas nécessaires aux lots 14A–14C et n'ont pas été repris.
- À l'inventaire initial, aucune branche ni PR Mission 13 n'était publiée; la PR #13 existante est Mission 8.5. Ensuite, la PR Mission 13 **#24** est apparue, ouverte en brouillon et mergeable, head `ff2c2fbbe04ea3e175ee7f864cc52978c3fb613b`, sœur de #22 sur la branche Mission 10 `codex/mission-10-autonomous-execution-integration-2026-11-24` au SHA `0cc54f77312d6fbf89e0f99c191f840bbddaf985`. Sa comparaison exacte à sa base montre huit chemins : trois modules `mission13_*`, son rapport, son fixture synthétique et trois tests Mission 13. Aucun ne chevauche les six chemins Mission 14. Le travail 14 reste isolé sur la branche #22 et évite également `paper_trader.py` et `backtest.py`.
- Le checkout `main` local contenait de nombreuses modifications suivies et non suivies. Il n'a pas été modifié. Le travail est dans un worktree séparé créé depuis le head vérifié de #22.

## 14A — Audit comptable

Le ledger de transactions confirmées reste séparé des simulations PAPER : ses lignes gardent `accounting_domain=CONFIRMED_CHAIN` et `applied_to_paper_pnl=false`. Il n'existe pas de callsite de production vers `record_confirmed_accounting`; les tests sont les seuls appelants observés.

La fonction de valorisation du ledger a été mise en fail-closed. Le schéma de valorisation passe à la version 2. Un taux, une source et un horodatage fournis par l'appelant, même avec `rate_verified=True`, ne suffisent plus : les montants USD sont `None` et le statut est `unknown` avec `rate_origin_unverifiable`. Les valeurs d'entrée restent annotées comme revendications de l'appelant, pas comme provenance vérifiée.

Le PnL PAPER courant n'est pas rendu complet par ce changement. `paper_trader.py` appelle encore `backtest.get_sol_usd_rate()`, qui retourne un `float` sans source ni timestamp et peut reprendre un cache périmé ou `config.SOL_USD_RATE`. Les frais PAPER sont simulés, et le chemin PAPER déclare ne pas simuler le slippage. Les totaux `total_pnl_usd` représentent le réalisé; aucun historique d'equity mark-to-market ni drawdown opérationnel complet n'est maintenu. Le warning existant `pnl_fee_history_incomplete` protège certaines données historiques à frais inconnus, mais ne prouve pas l'origine des taux.

Les sorties sans prix valide restent ouvertes/`unknown` selon la correction de Mission 11; aucune sortie fictive à prix d'entrée n'a été ajoutée. Les limites précédentes sur les frais réseau, frais prioritaires, loyer, tips et coûts fournisseur restent présentes.

**Décision 14D :** aucun raccord au PnL PAPER tant que le taux de prix n'apporte pas une origine fournisseur et une fraîcheur vérifiables, que les identifiants d'événement et les timestamps ne sont pas persistés, et que le calcul réalisé/non réalisé/equity n'a pas de contrat complet. La modification du chemin de trading risquerait de toucher aux mêmes surfaces que la Mission 13; elle reste gate fail-closed.

## 14B — Idempotence durable du ledger

Le détail reste limité à 1 000 lignes. Un index permanent de digests SHA-256 de signature et d'empreintes complètes des nouvelles données comptables est conservé dans le même document d'état que le ledger. Les deux sont écrits par le même save atomique. L'empreinte couvre l'objet comptable complet, y compris wallet, mints, deltas token, décimales et timestamp s'ils sont fournis. Un replay identique est ignoré même après éviction et redémarrage; un replay avec des données différentes est refusé. Les lignes héritées encore retenues sont migrées au prochain enregistrement en tombstones d'identité seulement : comme leurs anciens détails n'incluent pas tous les champs, elles ne peuvent pas être réconciliées ni promues en valorisations nouvelles.

Un `RLock` sérialise les appels passant par la même instance `DataStore`; c'est le seul périmètre de concurrence couvert par le test. `main.py` crée un `DataStore` par instance de `SniperBot`, mais aucun singleton inter-processus n'est imposé. Plusieurs instances ou processus partant du même snapshot peuvent donc tous deux accepter un événement puis s'écraser lors d'une sauvegarde complète, malgré l'atomicité individuelle des sauvegardes. L'idempotence n'est pas garantie dans ce scénario; une table événementielle transactionnelle ou un mécanisme de version/conflit avec rechargement est requis avant d'autoriser plusieurs writers. L'index permanent croît avec le nombre total de signatures. Les signatures évincées avant la migration Mission 14 ne peuvent pas être reconstituées rétroactivement si elles ne se trouvent plus dans les 1 000 lignes héritées.

La transition d'état UNKNOWN vers valued et son rollback sont couverts avec un validateur simulé pour exercer l'atomicité. En production, aucune preuve de taux fournisseur n'est disponible; la fonction réelle conserve donc ces valorisations à `UNKNOWN`. Le test simulé ne constitue pas une preuve de provenance.

## 14C — Backtesting et optimisation chronologiques

Le nouveau module `backtest_validation.py` propose une recherche de paramètres sur l'intervalle d'entraînement seulement, puis une unique évaluation chronologique sur une plage de validation séparée par un embargo configurable. La règle de signal reçoit un préfixe immuable qui se termine à la clôture évaluée; les entrées restent à l'ouverture suivante. Le rapport inclut PnL net, ROI, drawdown USD et relatif, taux de gain, profit factor net, frais et hypothèses de fills.

L'archive SHA-256 existante ne prouve pas l'origine d'un fournisseur. Par conséquent, tout dataset externe retourne `UNKNOWN` sans métriques. Les métriques sont disponibles uniquement pour une archive structurellement identifiée `synthetic`, marquée `SYNTHETIC_RESEARCH_ONLY` et `performance_claim_eligible=false`. Aucun dataset externe authentifié n'est présent dans le dépôt. Les coûts restent des scénarios de frais/slippage fixes; le modèle n'inclut pas réseau, priorité, loyer, tips, liquidité, impact ou latence et ne démontre pas une rentabilité réelle.

Le callback ne reçoit pas de bougies futures par cet API; le module ne peut toutefois pas prouver qu'un callable fermé sur d'autres données ou sur un état global est pur. Les métriques restent des sorties de recherche synthétique.

## Validation et accès

Validation locale sur le contenu Mission 14, avec Python 3.11 et dotenv désactivé :

- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHON_DOTENV_DISABLED=true python -m pytest -q tests/test_transaction_accounting.py tests/test_backtest_validation.py` : **49 passed**.
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHON_DOTENV_DISABLED=true python -m pytest -q` : **704 passed**.
- `python -m compileall -q .` : réussi.
- `mypy --follow-imports=silent transaction_accounting.py backtest_validation.py` : réussi, aucun diagnostic sur les deux modules.
- `mypy --follow-imports=silent monitoring_list.py` : 10 diagnostics de typage sur des signatures et un agrégat existants hors du bloc comptable modifié; aucun diagnostic sur les nouvelles fonctions de ledger.
- `git diff --check` : réussi.
- Ruff, `pip-audit`, Gitleaks et coverage ne sont pas installés localement. Une installation des dépendances CI a échoué à la vérification TLS; aucun contournement TLS n'a été tenté. Ces contrôles ont ensuite réussi dans la CI GitHub.
- CI GitHub [run 45509216983](https://github.com/cheicki225/-meme-coin/actions/runs/45509216983) réussie pour le head source `5022d4315461a3520e488e7259f10f624e270059` (base `9590196c40a64ef478a4d4e7c44ade5dee105f12`; commit de fusion temporaire CI `e6f313c32552bc62dad7b86904ae8fd79cd9cdd9`). Le run a réussi compile, mode PAPER, **704 tests**, Ruff critique, pip-audit (**aucune vulnérabilité connue**), Gitleaks (**aucune fuite**) et publication de coverage.
- Coverage combinée lignes + branches : **40,72 %** pour le dépôt; `backtest_validation.py` **81,58 %**, `transaction_accounting.py` **83,46 %**, `monitoring_list.py` **50,09 %**. Artefact : [mission-coverage-45509216983](https://github.com/cheicki225/-meme-coin/actions/runs/45509216983/artifacts/14974172289).
- Revue indépendante en lecture seule : les corrections sur embargo nul, tombstones legacy et métriques non finies sont confirmées. La revue a relevé que la concurrence entre instances/processus n'est pas garantie à cause des sauvegardes de snapshots complets; cette limite est déclarée ci-dessus et le test concurrent reste limité à une instance `DataStore`.

L'autoload des plugins pytest doit rester désactivé dans cet environnement : sans ce réglage, un plugin Python global `web3` échoue avant la collecte en important `ContractName` depuis `eth_typing`. Avec l'autoload désactivé, les suites ci-dessus s'exécutent sans erreur.

Tous les tests utilisent des fixtures synthétiques ou des chemins temporaires. Aucun portefeuille, clé, secret, endpoint RPC, fournisseur de marché, Telegram ou service LIVE n'a été consulté; aucune transaction, signature, opération de déploiement ou merge n'a eu lieu.

Éléments non vérifiés : soak PAPER cible de 24 h, restauration de l'environnement de déploiement, audit sémantique CPI/DEX, authenticité d'un dataset externe, origine/fraîcheur du taux SOL/USD du runtime et equity/drawdown mark-to-market en PAPER.

## Prochaine étape

Les preuves de code et de CI ci-dessus portent sur le SHA Mission 14 `5022d4315461a3520e488e7259f10f624e270059`; les commits ultérieurs ne modifient que ce rapport. L'état CI du head courant est visible sur la [PR #25](https://github.com/cheicki225/-meme-coin/pull/25). Le head #22 était `9590196c40a64ef478a4d4e7c44ade5dee105f12` lors de la dernière vérification. Aucune PR ne sera fusionnée automatiquement.
