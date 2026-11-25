# Mission 21 — Profilage PAPER hors ligne

**Statut : `PARTIALLY_READY / MANUAL_VERIFICATION_REQUIRED`**  
**Date : 2026-11-25**

Cette livraison prépare un profil reproductible du stockage d’événements sur SQLite. Elle ne mesure ni le bot complet, ni un fournisseur RPC, ni les frais réseau ou blockchain. Aucun soak PAPER de 24 h ou exercice sur l’environnement cible n’a été exécuté.

## Base, dépendances et isolation

- Dépôt : `cheicki225/-meme-coin`.
- Base : PR brouillon #33 Mission 19, branche `codex/mission-19-data-integrity-20261125`, SHA `d66793d417edd78fac35b448c9effcce9cf9cca7`.
- La CI `45719579840` est réussie sur ce SHA de base; elle ne valide pas Mission 21.
- PR brouillon #34 : branche `codex/mission-21-paper-profiler-20261125`, ouverte initialement sur le commit code/test `29f2ede986b8467ae4e29ba8a971fd5bc7389690`, empilée sur #33.
- Worktree isolé : `C:\Users\LENOVO\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\BOT-MEME-COIN-MISSION21-20261125`.
- `scripts/mission21_sqlite_profile.py` et son test sont les seuls fichiers code/test ajoutés pour M21.
- Le profil crée sa base WAL et son fichier de télémétrie dans un dossier temporaire neuf, borne les événements à 5 000, refuse de réutiliser ses propres noms de fichiers, puis nettoie le dossier. Le checkout utilisateur, les bases configurées, les secrets et les branches sources n’ont pas été utilisés.

## Méthode

Le script exerce `HeliusEventStore` (schéma 7) avec des enregistrements synthétiques contenant chacun un payload JSON de 115 octets :

1. insertion de 1 000 identités uniques avec ordre synthétique explicite;
2. réinsertion des 1 000 mêmes identités pour mesurer la déduplication;
3. claims et ACK séquentiels jusqu’au checkpoint 1 000;
4. contrôle `integrity_check`, des compteurs, de l’état final et des fichiers SQLite/WAL.

Le mode est imposé à `PAPER`; dotenv est désactivé. Le script n’ouvre aucun client réseau et n’importe pas la configuration d’exécution du bot. La télémétrie asynchrone de stockage écrit seulement dans le répertoire temporaire.

## Résultat mesuré

La commande `python scripts/mission21_sqlite_profile.py --events 1000`, exécutée sur le commit code/test `29f2ede986b8467ae4e29ba8a971fd5bc7389690`, a utilisé Python 3.11.9, SQLite 3.45.1 et Windows 10 (build 26200). Les percentiles sont des millisecondes par appel, mesurés sur une seule exécution locale.

| Phase | Nombre | p50 ms | p95 ms | p99 ms | Débit observé |
|---|---:|---:|---:|---:|---:|
| Insertion unique | 1 000 | 1,923 | 3,026 | 5,069 | 471,65 opérations/s |
| Réinsertion dédupliquée | 1 000 | 1,396 | 2,137 | 4,497 | 641,60 opérations/s |
| Claim | 1 000 | 2,158 | 3,341 | 5,414 | 225,16 événements/s pour le cycle claim/ACK complet |
| ACK | 1 000 | 1,941 | 3,533 | 5,364 | inclus dans le cycle ci-dessus |

À la fin du profil : 1 000 lignes uniques, 1 000 observations de doublon, 1 000 lignes traitées, aucune ligne pending ou UNKNOWN, checkpoint 1 000, intégrité `ok`. Les composantes SQLite/WAL présentes faisaient 4 648 584 octets au total (DB 466 944; SHM 32 768; WAL 4 148 872), soit 4 648,58 octets par événement unique pour cet instantané avec WAL. Le fichier télémétrie temporaire séparé faisait 329 568 octets. Ce chiffre inclut les journaux WAL du workload et ne représente pas une taille compacte après checkpoint.

Les chiffres décrivent uniquement ce poste, cette version de SQLite, ce profil et le code d’inbox M19. Ils ne fixent aucun SLO ou capacité de production. Les frais fournisseurs et blockchain sont `not measured`; aucun swap ni coût fictif en SOL/USD n’a été calculé.

## Vérifications locales

Les commandes suivantes ont été exécutées après le commit code/test `29f2ede986b8467ae4e29ba8a971fd5bc7389690` :

- `python -m compileall -q .` — réussi.
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q tests/test_mission21_sqlite_profile.py tests/test_helius_event_store.py` avec `TRADING_MODE=PAPER`, `LIVE_EXECUTION_ENABLED=false` et `PYTHON_DOTENV_DISABLED=true` — `63 passed`.
- Même environnement, `python -m pytest -q` — `899 passed` en 25,23 s.
- `git diff --check` avant commit — réussi.
- `mypy --follow-imports=silent scripts/mission21_sqlite_profile.py` avec mypy 1.7.1 — aucune erreur.
- Ruff, pip-audit, Gitleaks et coverage — outils locaux indisponibles; ils restent à vérifier sur la CI du SHA exact.
- Une exécution initiale du CLI a échoué avant de produire une mesure (résolution du module racine et fermeture du répertoire temporaire sous Windows). Les deux cas ont été corrigés avant les validations et la mesure rapportée.

## Préparation opérationnelle et gates

La PR #31 Mission 20 conserve son gabarit de collecte d’évidence opérateur. Mission 19 demeure `PARTIALLY_READY / MANUAL_VERIFICATION_REQUIRED`; la validation M20 opérationnelle reste donc bloquée. Aucun soak de 24 h, restauration de la cible, démarrage de service, provider, compte ou environnement n’a été touché.

Prochaines preuves nécessaires avant readiness opérationnelle : connexion par un opérateur à l’environnement PAPER autorisé, runbook de sauvegarde/restauration approuvé, restauration exercée sur la cible, soak réellement observé pendant 24 h avec états de redémarrage et métriques conservés, puis revue indépendante des findings P1/P2. Aucune mesure locale ne remplace ces éléments.

Le seul coût décrit ici est l’empreinte locale SQLite/WAL/telemetry; le profil ne simule ni ne vérifie le montant des frais Jupiter, RPC, Helius ou de chaîne. La CI exacte de la tête finale de PR #34 doit être consultée avant toute déclaration de validation CI.

## Périmètre d’effet

Aucune clé, wallet, donnée de compte, URL RPC, provider, signature réelle, transaction, ordre, dépense, endpoint LIVE, déploiement, merge ou modification de limites de risque. Toutes les identités et charges du profil sont synthétiques et le code n’est pas raccordé à `main.py`/`PaperTrader`.
