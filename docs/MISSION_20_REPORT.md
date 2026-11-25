# Mission 20 — Préparation de la validation PAPER opérateur

**Statut : `BLOCKED / MANUAL_VERIFICATION_REQUIRED`**  
**Date : 2026-11-25**

La Mission 20 a produit le runbook de collecte d’évidence et un validateur local de la structure d’un export anonymisé. Elle n’a pas validé un déploiement, une restauration de la cible ou un soak de 24 heures.

## Base et PR

- Dépôt : `cheicki225/-meme-coin`.
- Base PR #30 Mission 18 : `codex/mission-18-core-paper-integration-20261125`, SHA `e9334d5716b7fd5969cae3f077bb9fc1a2a1d1d7`.
- PR brouillon #31 : `codex/mission-20-readiness-prep-20261125`, head code/tests `36bf8fc5b9e179f1dbe94ddbff42f32e69189827`; CI run `45722663806` en cours à la rédaction. Le run `45713658164` réussi concernait l’ancien head documentaire `b65ed1f33924405224ea9f6b7f1b6db6c0f6d8a3`, pas le code M20 courant.
- La PR demeure brouillon et non fusionnée. Aucune branche source, cible, base configurée ou secret n’a été modifié.

## Livrables

- `docs/MISSION_20_OPERATOR_EVIDENCE_TEMPLATE.md` : gate d’entrée M19, identité du run, relevé horaire, registre d’arrêt, sauvegarde/restauration et closeout. Les résultats demeurent `NOT RUN` tant qu’ils ne sont pas appuyés par des références directes de la cible PAPER autorisée.
- `scripts/mission20_validate_operator_evidence.py` : vérifie le schéma d’un export JSON fourni explicitement, les dates UTC, la durée enregistrée, les intervalles d’observation, les flags PAPER, les compteurs UNKNOWN/429, les références expurgées et les champs de restauration. Il n’ouvre aucune base SQLite, ne contacte aucun service et n’écrit aucun fichier.
- `tests/test_mission20_operator_evidence.py` : fixtures déterministes couvrant format complet, gate M19 ouverte, durée/gaps invalides, PAPER/LIVE, UNKNOWN, références et restauration vers un nouveau chemin.
- Le dépôt possède déjà `scripts/paper_release_readiness.py` et `tests/test_datastore_recovery.py`. Le premier ne fait que des contrôles locaux read-only quand l’opérateur lui passe des chemins; le second couvre des reprises isolées, dont un redémarrage simulé. Aucune de ces vérifications ne prouve une restauration sur la cible.

Même un export structurellement valide renvoie toujours `MANUAL_VERIFICATION_REQUIRED` et `operational_evidence_verified=false`. Le champ `mission19_gate` est une déclaration de l’opérateur, pas une preuve vérifiée par l’outil. Une fixture de 24 h teste uniquement le validateur et ne constitue pas un soak.

## Vérifications exécutées sur le code/tests SHA `36bf8fc5b9e179f1dbe94ddbff42f32e69189827`

Avec `TRADING_MODE=PAPER`, `LIVE_EXECUTION_ENABLED=false`, `PYTHON_DOTENV_DISABLED=true` et `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` :

| Vérification | Résultat |
|---|---|
| `python -m pytest -q tests/test_mission20_operator_evidence.py tests/test_paper_release_readiness.py tests/test_datastore_recovery.py` | `34 passed` |
| `python -m pytest -q` | `853 passed` en 23,59 s |
| `python -m compileall -q .` | Réussi |
| `mypy --follow-imports=silent scripts/mission20_validate_operator_evidence.py` | mypy 1.7.1, aucune erreur |
| `git diff --check` avant commit | Réussi |

## Gates et limites

- Mission 19 reste `PARTIALLY_READY / MANUAL_VERIFICATION_REQUIRED` : l’exhaustivité indépendante de l’historique RPC, la vérification du feed/signature SOL/USD et le chemin atomique admission/position/PnL ne sont pas validés. La gate M20 interdit donc tout run sur cible dans cette étape.
- Aucun processus du bot, provider, RPC, Telegram, wallet, compte, base cible, sauvegarde de production, service ou configuration cible n’a été consulté.
- Aucun soak de 24 h n’a été démarré ou observé; aucune restauration de cible n’a été exécutée.
- Le validateur ne peut ni authentifier les horodatages, ni inspecter le contenu référencé, ni prouver la continuité murale. La preuve opérationnelle et l’approbation d’entrée restent manuelles.
- Le workflow CI GitHub se déclenche sur un push/Pull Request; le run sur le SHA `36bf8fc…` était en cours. La tête documentaire finale après ce rapport exige une nouvelle vérification de CI exacte.

**Conclusion :** les outils et formulaires de préparation sont présents et testés offline. L’acceptation M20 reste `BLOCKED / MANUAL_VERIFICATION_REQUIRED` jusqu’à la levée des gates M19 et une autorisation opérationnelle distincte.

## Revue indépendante et correction du code de sortie

La revue indépendante du head précédent a trouvé une ambiguïté P2 : un record bien formé pouvait produire le code de sortie `0` même si `mission19_gate` n’était pas `CLEARED`. La validation de format pouvait donc être confondue avec une validation opérationnelle par un appelant qui ne lit que le code de sortie.

Le commit code/tests `ffa30265d790391f00bcfadce14f254179c0c442` corrige ce signal : le CLI retourne `2` pour une entrée invalide/illisible et `3` si le schéma est valide mais que la preuve opérationnelle n’est pas explicitement vérifiée. Comme le validateur actuel garde toujours `operational_evidence_verified=false`, il ne retourne jamais `0`. Deux régressions couvrent ces codes.

Vérifications locales après correction, avec plugins pytest tiers désactivés pour éviter l’incompatibilité préexistante `web3`/`eth_typing` de l’installation globale :

| Vérification | Résultat |
|---|---|
| Tests M20 ciblés + readiness locale + reprise DataStore | `36 passed` |
| Suite complète | `855 passed` en 22,62 s |
| `python -m compileall -q .` | Réussi |
| `mypy --follow-imports=silent scripts/mission20_validate_operator_evidence.py` | Aucune erreur |
| `git diff --check` | Réussi |

La CI du nouveau head final doit être relue sur la PR #31 avant de rapporter ce correctif comme validé par GitHub. Aucun soak, accès cible, fournisseur, base cible ou processus du bot n’a été ajouté à cette revue.
