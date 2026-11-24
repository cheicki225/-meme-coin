# Mission 15 — Dashboard, Telegram et monitoring PAPER

**Statut : PARTIALLY_READY / MANUAL_VERIFICATION_REQUIRED**

**Branche dédiée :** `codex/mission-15-dashboard-telegram-monitoring-20261124`

**Base vérifiée :** PR #21, commit `0cc54f77312d6fbf89e0f99c191f840bbddaf985`

**Checkout :** clone isolé `BOT-MEME-COIN-MISSION15-20261124`
**Aucun merge effectué.**

## Périmètre et contrat d'intégration

Le checkout local `main` fourni était très modifié et contenait un `.env`. Il n'a pas été lu ni modifié. Mission 15 utilise un clone neuf et une branche créée à partir du HEAD exact de la PR #21. Les PR #22, #23 et #24 restent des branches sœurs ouvertes, toutes basées sur #21. Le checkout local de Mission 14 était déjà modifié à partir de `9590196c40a64ef478a4d4e7c44ade5dee105f12`; il n'a pas été modifié. Lors de l'inventaire initial, aucune PR Mission 14 n'était visible.

Pendant la mission, la PR brouillon Mission 14 est apparue sous le numéro **#25**, HEAD `369c872e1b74c92112bb04f6d90b937d5f215a13`, basée sur la PR #22 à `9590196c40a64ef478a4d4e7c44ade5dee105f12`. Son check `validate` réussit sur ce HEAD (run `45509950082`). Elle touche `backtest_validation.py`, `monitoring_list.py`, `transaction_accounting.py` et leurs tests/rapport; aucun de ces chemins ne fait partie de Mission 15. PR #21 est restée à `0cc54f77312d6fbf89e0f99c191f840bbddaf985` pendant la préparation de cette branche.

Pour éviter les conflits et les doublons, Mission 15 n'altère pas `main.py`, `telegram_bot.py`, `monitoring_list.py`, `helius_event_store.py`, `execution_metrics.py`, `paper_trader.py` ou `transaction_accounting.py`. Les changements sont limités à un nouveau viewer local, ses tests et ce rapport. Une intégration ultérieure dans le processus du bot devra être une modification distincte, revue avec les branches Missions 11–14.

## 15A — Audit des interfaces existantes

- Aucun dashboard web n'existait dans le checkout #21. `pnl_card.py` crée une carte PnL Telegram; ce n'est pas un dashboard.
- Telegram est l'interface existante. Les commandes `/stats`, `/positions`, `/performance`, `/reconcile`, `/halt`, `/resume` et `/activity` sont déjà présentes. Les callbacks passent par l'autorisation fail-closed et des tests dédiés existent. Aucun changement à ces commandes ou à leurs contrôles n'est introduit ici.
- `RpcHealthMonitor` est relié au runtime et envoie des alertes fondées sur les compteurs du client RPC; ce n'est pas une sonde active ni un snapshot persistant de santé.
- `HeliusEventStore` est relié aux listeners; il persiste files, reprises et checkpoints. Les métriques existantes comptent notamment doublons, replays, retries, inconnus et refus de file.
- Les métriques d'exécution sont filtrées et écrites de façon best-effort dans un JSONL. Les compteurs RPC et l'état de livraison Telegram restent en mémoire ou ne sont pas persistés.
- Aucun planificateur fiable de rapports périodiques n'a été trouvé; aucun n'est ajouté.

## 15B/15D — Viewer local et limites de visibilité

`paper_dashboard.py` sert une page et `/api/status` uniquement sur `127.0.0.1`. Le serveur accepte les lectures GET prévues; POST/PUT/PATCH/DELETE répondent 405. Les données proviennent des chemins locaux fournis à la ligne de commande :

```powershell
python paper_dashboard.py --state-path sniper_data.sqlite3 --helius-db data/helius_events.sqlite3 --metrics-file data/execution_metrics.jsonl
```

Les chemins peuvent être changés pour correspondre aux chemins locaux du runtime. Le module n'importe pas `config`, ne charge pas dotenv, n'inspecte pas l'environnement et n'affiche pas les chemins sources. Il lit les bases SQLite avec `mode=ro` et `query_only`, limite la taille lue du JSONL, valide les labels métriques par liste blanche et renvoie uniquement des nombres agrégés. Les noms de streams, signatures, wallets, mints, payloads, URL, texte d'erreur, secrets et chemins ne sont pas servis.

Chaque point retourne un statut, une source statique et un timestamp de fichier ou de dernier événement lorsque disponible. Aucun seuil de fraîcheur n'est défini par ce changement; le viewer affiche les timestamps sans déclarer automatiquement un flux frais ou en panne.

Le mode runtime, le heartbeat, la santé RPC, la santé du fournisseur Helius, la livraison Telegram, l'état des signaux et le PnL non réalisé restent `UNKNOWN` lorsque ces valeurs ne sont pas persistées. Les compteurs Helius incluent le pending, les bytes, received/processed/unknown, duplicates/replayed/retried/queue-full et les agrégats de checkpoints/recovery/pause. Le timestamp du dernier événement reçu/traité est exposé sans conclure à sa fraîcheur. Les erreurs visibles sont uniquement les outcomes failure/unknown de la télémétrie assainie; le viewer ne lit aucun log brut et ne compte pas toutes les erreurs runtime. Le nombre RPC 429 reste `UNKNOWN` faute de compteur persistant.

Le total PnL et les frais sont des valeurs agrégées déjà persistées, non recalculées. La complétude des frais est montrée séparément. Le mode historique des trades n'est pas enregistré dans ces sources; la page n'affirme donc pas qu'un état persistant provient de PAPER. L'exposition présentée est la somme du coût de revient stocké, pas une valeur mark-to-market.

## 15C — Telegram

Les commandes existantes couvrent déjà statut, positions, performance, activité et arrêt. Le contrôle d'accès et les chemins de callback existants sont conservés. Aucune commande n'active LIVE; aucune commande de reprise, notification périodique ou alerte n'est ajoutée dans cette branche, afin de ne pas dupliquer le travail des PR sœurs ni de réimplémenter leurs contrôles. Les tests Telegram existants seront inclus dans la validation locale.

## 15E — Validation

Les validations ci-dessous doivent correspondre au HEAD final de cette branche. Les tests ciblés déjà exécutés sont :

- Python 3.11.9.
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `PYTHON_DOTENV_DISABLED=true`, `python -m pytest -q tests/test_paper_dashboard.py` : **14 réussis**.
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `PYTHON_DOTENV_DISABLED=true`, `python -m pytest -q` : **636 réussis**.
- `PYTHON_DOTENV_DISABLED=true`, `python -m compileall -q .` : réussi.
- Le premier essai pytest sans désactivation des plugins a échoué pendant le chargement d'un plugin pytest global `web3` incompatible avec `eth_typing.ContractName`; `TESTING.md` décrit ce problème local et autorise ce contournement.
- La revue indépendante en lecture seule a trouvé des cas P2 liés aux fenêtres métriques tronquées, timestamps métriques, versions d'imputation des frais et historique de trades sans positions associées. Ils ont été corrigés et couverts par des tests; le contrôle final n'a trouvé aucun autre problème. Il ne s'agit pas d'une approbation GitHub.
- Un test SQLite WAL garde un writer de fixture ouvert pendant la lecture et vérifie que la base, le `-wal` et le `-shm` conservent leur taille.
- `coverage`, `ruff`, `pip-audit` et `gitleaks` ne sont pas installés dans l'environnement Python/terminal local; aucun paquet n'a été installé. Ces étapes restent à prouver sur le HEAD final via GitHub Actions. La couverture locale n'a donc pas été mesurée.

## Sécurité, retour arrière et preuves manquantes

Le viewer n'offre aucun contrôle du bot et ne contacte ni RPC/Helius/Telegram ni wallet. Il n'active pas LIVE, ne signe ni ne transmet de transaction, ne fait aucun appel payant et ne déploie rien. Le bind loopback réduit l'accès réseau, mais toute personne ou application ayant accès à la session locale peut consulter les agrégats affichés; ne pas partager cette page ou ses réponses API.

Le retour arrière est le revert des commits Mission 15 sur cette branche dédiée. Les branches #21–24 et le checkout source `main` restent indépendants; aucun merge automatique n'est prévu.

Restent hors preuve : raccordement dans le processus du bot, santé RPC/Helius/Telegram en temps réel, frais historiques complets, signaux canoniques, métriques 429 persistées, exécution PAPER dans l'environnement cible, exercice de restauration cible, soak de 24 h, audit sémantique DEX/CPI et toute fiabilité de production. La CI verte et les tests hors ligne ne remplacent pas ces vérifications.
