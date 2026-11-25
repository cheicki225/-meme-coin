# Mission 18 — Core PAPER Integration

Date: 2026-11-25
Status: **PARTIALLY_READY / MANUAL_VERIFICATION_REQUIRED**

## Résumé

Mission 18 ajoute des primitives SQLite hors ligne pour réserver atomiquement une identité et de l'exposition PAPER, enregistrer un état de position dans la même transaction, et afficher des comptes de réservations dans le dashboard en lecture seule. Les données synthétiques peuvent être évaluées pour inspecter le score et le régime, mais le résultat reste explicitement bloqué avec provenance `UNKNOWN`; il ne constitue pas un permis et ne crée aucune réservation.

La route runtime reste bloquée. Les sources Helius live n'apportent pas l'index canonique requis; la fraîcheur Wallet Intelligence et GoPlus n'est pas authentifiée; aucun fournisseur SOL/USD ne fournit ici une preuve indépendante de source, d'horodatage et de fraîcheur. En conséquence, cette mission ne raccorde pas les signaux à l'admission runtime, n'autorise pas de buy/sell PAPER valorisé, et ne calcule aucun PnL à partir d'un prix non vérifié. Aucune validation live, soak de 24 h ni audit DEX/CPI n'est revendiqué.

## Dépôt, base et isolation

- Dépôt : `cheicki225/-meme-coin`.
- Le checkout utilisateur `C:\Users\LENOVO\OneDrive\Desktop\mon bot de traveil\BOT DE MEME COIN` contenait déjà des modifications et fichiers non suivis, dont `.env`. Il n'a pas été modifié.
- Clone isolé : `C:\Users\LENOVO\AppData\Local\BOT-MEME-COIN-MISSION18-20261125`.
- Branche de travail : `codex/mission-18-core-paper-integration-20261125`.
- Base vérifiée : head de la PR #29, `1698ae1116dd28d17e9264abb3145c383c3dd5dd`; PR #29 est un brouillon ouvert empilé sur la branche Mission 17 `codex/mission-17-final-paper-rc-20261125`. Aucun changement n'a été apporté à `main`, à #29 ou aux branches sources.
- Aucun pull request Mission 18 n'existait au prévol. Les PR #21, #22–#26, #28 et #29 étaient ouvertes en brouillon, sans modification de leurs heads lors du contrôle; leurs workflows PR associés étaient terminés avec succès :

| PR | Head vérifié | Workflow PR vérifié |
|---|---|---|
| #21 | `0cc54f77312d6fbf89e0f99c191f840bbddaf985` | `45489737659` — succès |
| #22 | `9590196c40a64ef478a4d4e7c44ade5dee105f12` | `45500132578` — succès |
| #23 | `ba11a28c90f071a678a8d5b1c51b612b203f2988` | `45501875324` — succès |
| #24 | `ff2c2fbbe04ea3e175ee7f864cc52978c3fb613b` | `45506727076` — succès |
| #25 | `369c872e1b74c92112bb04f6d90b937d5f215a13` | `45509950082` — succès |
| #26 | `97cef75449295673f999e29c64fac35fefe0b6d1` | `45512772101` — succès |
| #28 | `8cbaa8f6b95186e10b58b2de5cba5b2c3e22b1ae` | `45572715499` — succès |
| #29 | `1698ae1116dd28d17e9264abb3145c383c3dd5dd` | `45683700863` — succès |

Les états et SHAs ci-dessus sont des constats du prévol; ils ne remplacent pas le contrôle du head final de cette branche ni de sa propre CI.

## Gates de Mission 18

| Gate | Résultat | Éléments vérifiés / limite |
|---|---|---|
| 18A — Helius, Wallet Intelligence, GoPlus, scoring | **BLOCKED** | Helius live n'a pas d'index canonique dans le callback; les champs source/provenance et heures d'observation du scoring sont caller-side; GoPlus n'expose pas ici un timestamp fournisseur vérifiable. Les signaux non canoniques, manquants ou non authentifiés restent `UNKNOWN` et ne sont pas réservés. |
| 18B/C — Identité, exposition atomique, stop, reprise | **PARTIAL** | Le store SQLite hors ligne utilise `BEGIN IMMEDIATE`, contrainte unique signal/corrélation, contrôle du stop/pertes/limites lors de la réservation, réservation active des états `pending`/`unknown`, et commit atomique de position + révision + réservation. Les primitives ne sont pas raccordées aux callsites runtime et n'authentifient pas la provenance; elles ne débloquent donc pas l'admission. |
| 18D/E — Buy/sell simulés, prix, frais et PnL | **BLOCKED** | Aucun fournisseur SOL/USD avec provenance, timestamp source et fraîcheur vérifiables n'a été intégré. Les chemins existants échouent fermés; aucun prix statique, périmé ou caller-side n'est traité comme preuve. Ledger chaîne confirmé et PnL PAPER restent distincts. |
| 18F — Dashboard et Telegram | **PARTIAL** | Le dashboard local peut lire des comptes agrégés de réservations avec son accès SQLite en lecture seule. Il ne montre aucune identité et conserve `signals=UNKNOWN`. Telegram reste dans son comportement existant d'affichage `UNKNOWN`; il n'est pas raccordé au nouveau store. |
| 18G — Tests et contrôles | **PASS local limité** | 846 tests hors ligne passent; compileall, mypy ciblé et `git diff --check` passent. Ruff, pip-audit, Gitleaks et Coverage ne sont pas installés localement. La CI de cette branche doit être consultée sur son SHA exact avant de qualifier le gate externe. |

## Changements

- `mission18_paper_admission.py` ajoute `SQLitePaperAdmissionStore`, un stockage additif dans la base `bot_state`. Le prévol refuse un schéma de base non pris en charge avant activation de WAL ou création de table. Snapshot, réservation et commit vérifient les deux versions SQLite (`PRAGMA user_version` et `bot_state.schema_version`) contre la version prise en charge.
- La réservation sérialise les processus SQLite, réserve identités et exposition avant effet, et conserve les états `pending`/`unknown` après redémarrage. Un caller ne peut pas libérer une réservation par simple assertion `effect_not_started`. Sans preuve durable ni réconciliateur autorisé, ces états restent bloqués. Le commit contrôle à nouveau l'arrêt d'urgence et utilise la révision d'état pour refuser un snapshot périmé.
- `evaluate_paper_candidate_policy_offline` retourne un résultat d'inspection qui expose séparément l'évaluation de politique et l'état d'admission bloqué. Le résultat porte `provider_provenance_status=UNKNOWN`, `runtime_permitted=False`, aucune réservation, et le temps caller est signalé comme non vérifié.
- `paper_dashboard.py` accepte une source de comptes facultative et n'en lit que les agrégats via l'accès lecture seule déjà utilisé; absence, invalidité ou incohérence laisse l'état dégradé/UNKNOWN.
- `tests/test_mission18_paper_admission.py` couvre l'évaluation synthétique bloquée, les identités/corrélations persistantes, limites et arrêt, limites de slots et d'exposition en concurrence multiprocessus, crash/restart, commit position/réservation, conflit de révision, arrêt survenu avant commit, refus de libération par assertion, et refus de schéma futur au constructeur, au snapshot, à la réservation et au commit.
- `tests/test_mission18_dashboard.py` vérifie le caractère agrégé et lecture seule du statut, ainsi que le comportement en cas de source absente ou invalide.

## Vérifications exécutées

Environnement : Windows, Python 3.11.9. Tests exécutés avec `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `PYTHON_DOTENV_DISABLED=true`, `TRADING_MODE=PAPER` et `LIVE_EXECUTION_ENABLED=false`.

| Commande | Résultat |
|---|---|
| `python -m pytest -q tests/test_mission18_paper_admission.py tests/test_mission18_dashboard.py` | **18 passed** |
| `python -m pytest -q` | **846 passed in 23.59s** |
| `python -m compileall -q .` | réussi |
| `python -m mypy --follow-imports=silent mission18_paper_admission.py` | `Success: no issues found in 1 source file` |
| `git diff --check` | réussi avant ajout de ce rapport; à rejouer sur le commit final |
| Disponibilité locale `ruff`, `pip-audit`, `gitleaks`, `coverage` | aucun exécutable trouvé; aucun paquet installé |

Le chargement automatique de plugins pytest a été désactivé après une erreur de pré-collecte liée à une installation globale `web3` qui attend un symbole `ContractName` absent de `eth_typing`. Ce problème d'environnement n'a pas été corrigé ni masqué dans les dépendances du dépôt.

La CI du dépôt utilise Python 3.11, compileall, une vérification du mode PAPER par défaut, pytest avec couverture, Ruff (erreurs critiques), pip-audit et Gitleaks. Aucun résultat de cette CI ne sera revendiqué avant son exécution sur le SHA Mission 18 exact.

## Revue indépendante

Trois audits de lecture seule ont examiné séparément l'admission/risque, Helius et Wallet Intelligence, puis les surfaces prix/PnL/dashboard/Telegram. Une revue indépendante finale du code modifié a confirmé la vérification de schéma avant WAL/DDL, les refus au stop d'urgence, l'absence de libération fondée uniquement sur une assertion du caller, et le test de course d'exposition multi-processus. Aucun défaut P1/P2 restant n'a été trouvé dans le périmètre ciblé. La revue confirme toutefois que les mutateurs SQLite ne prouvent pas eux-mêmes la provenance; les appeler depuis une route runtime reste bloqué jusqu'à l'ajout d'une preuve authentifiée.

## Sécurité et limites

- Aucun secret, `.env`, clé, wallet ou identifiant sensible n'a été utilisé pour l'intégration. Aucun fournisseur, service payant ou endpoint RPC n'a été appelé.
- Aucun ordre, transaction signée, appel RPC d'écriture, transfert, déploiement externe, soak de 24 heures ou audit DEX/CPI n'a été effectué.
- Le développement et les tests sont PAPER/offline. Aucune autorisation d'exécution live n'existe.
- La réservation de bas niveau accepte des identifiants et limites fournis par son appelant; elle ne prouve ni provenance fournisseur ni validité runtime. Elle demeure un primitive hors ligne, sans callsite connecté. Ne pas la traiter comme admission authentifiée.
- Le rapprochement des réservations `pending`/`unknown` n'est pas automatisé : une preuve de l'effet simulé et un protocole de réconciliation durable sont requis. Aucun état ambigu n'est libéré par défaut.
- Le nouveau résultat synthétique peut montrer qu'une politique fournie attribuerait un score/régime, mais son état reste bloqué. Ce résultat ne démontre ni qualité de signal, ni performance, ni readiness opérationnelle.

## Retour arrière et suite

Les changements sont isolés dans la branche Mission 18. Un rollback consiste à fermer le brouillon et conserver clone, branche et rapport; ne pas nettoyer le checkout d'origine ni réécrire les branches antérieures. Les ajouts SQLite sont conservés si une base future les a utilisés; ne supprimer aucune donnée ni table sans sauvegarde et revue opérateur.

Prochaine étape : revue humaine de la PR brouillon et des gates CI exactes. Garder l'admission runtime, les achats/ventes PAPER, le PnL et toute exécution LIVE bloqués jusqu'à preuve de provenance, raccord durable des signaux, fournisseur de prix vérifiable, réconciliation après crash et validation opérateur ciblée.
