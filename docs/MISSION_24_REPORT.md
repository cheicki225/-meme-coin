# Mission 24 — Verified Market Data & PAPER Recovery

**Date : 2026-11-26**

**Statut : `PARTIALLY_READY / MANUAL_VERIFICATION_REQUIRED`**

**Dépôt :** `cheicki225/-meme-coin`

**Branche :** `codex/mission-24-verified-market-data-paper-recovery-20261126`

**Base :** PR #35, `75537f966a068e1379fb4a33d4be9481971cbd00`

## Résumé

Mission 24 améliore l’interruption des lectures de reprise Helius, ajoute des preuves de crash/rollback pour les transactions SQLite PAPER et vérifie que le dashboard conserve les admissions `UNKNOWN` comme dégradées après redémarrage. Aucune donnée n’est promue en « vérifiée » par une déclaration de code appelant.

Les blocs critiques de confiance restent ouverts. Helius fournit des champs de slot/index et de commitment rapportés par le provider; cela n’établit pas une preuve indépendante de l’authenticité ou de la complétude historique. Le callback CopyTrade réel ne fournit pas le `PaperCandidateRequest` M13 requis et garde l’entrée `UNKNOWN`; le runtime M23 reste SIMULATED-only. La provenance SOL/USD n’a pas de vérificateur compatible démontré et reste `UNKNOWN`. Les voies d’admission live et de comptabilité USD validée restent fermées.

## Prévol GitHub et worktrees

Vérifié le 2026-11-26 via GitHub : #29–#35 sont ouverts, en brouillon et non fusionnés. Les heads de #29–#35, les bases, et leurs runs CI exacts n’avaient pas avancé. #21 et #28, dépendances antérieures, restent également ouvertes et non fusionnées. Aucune PR/branche Mission 24 n’existait dans les PR récentes ou les références locales/distance avant ce travail.

| PR | Base observée | Head observé | CI sur ce SHA |
| --- | --- | --- | --- |
| [#29](https://github.com/cheicki225/-meme-coin/pull/29) | #28 — `8cbaa8f6b95186e10b58b2de5cba5b2c3e22b1ae` | `1698ae1116dd28d17e9264abb3145c383c3dd5dd` | `45683700863` — succès |
| [#30](https://github.com/cheicki225/-meme-coin/pull/30) | #29 — `1698ae1116dd28d17e9264abb3145c383c3dd5dd` | `e9334d5716b7fd5969cae3f077bb9fc1a2a1d1d7` | `45701063429` — succès |
| [#31](https://github.com/cheicki225/-meme-coin/pull/31) | #30 — `e9334d5716b7fd5969cae3f077bb9fc1a2a1d1d7` | `488c9c389289945463faa4789e3d28756e46da79` | `45724713897` — succès |
| [#32](https://github.com/cheicki225/-meme-coin/pull/32) | #30 — `e9334d5716b7fd5969cae3f077bb9fc1a2a1d1d7` | `138d9aced2ecaae13d2192ae6d676f1e9cd6d2c3` | `45725669706` — succès |
| [#33](https://github.com/cheicki225/-meme-coin/pull/33) | #30 — `e9334d5716b7fd5969cae3f077bb9fc1a2a1d1d7` | `d66793d417edd78fac35b448c9effcce9cf9cca7` | `45719579840` — succès |
| [#34](https://github.com/cheicki225/-meme-coin/pull/34) | #33 — `d66793d417edd78fac35b448c9effcce9cf9cca7` | `8fe16516d013242cf36cbfb2b07affb9bc8dc99a` | `45721468111` — succès |
| [#35](https://github.com/cheicki225/-meme-coin/pull/35) | #29 — `1698ae1116dd28d17e9264abb3145c383c3dd5dd` | `75537f966a068e1379fb4a33d4be9481971cbd00` | `45784151258` — succès |

DAG constaté :

```text
#21 → #28 → #29 → #30 ─┬→ #31
                       ├→ #32
                       └→ #33 → #34
                 #29 ───────→ #35 → Mission 24
```

La Mission 24 a été créée dans un worktree distinct basé sur le head exact de #35. Les PR sources ne sont pas modifiées. Le worktree principal du clone isolé avait déjà des suppressions staged et un répertoire Mission 23 non suivi; il n’a pas été utilisé pour les modifications. Le checkout utilisateur séparé `main`, déjà modifié, a été laissé intact. Aucun fichier `.env` ou secret n’a été lu.

## Travaux par domaine

### 24A — Reprise Helius

`helius_event_store.py` utilise maintenant un helper d’attente annulable pour les lectures read-only de borne processed, les lectures de slot finalized, l’attente de finalité et la pagination historique. Lorsque stop est demandé, la tâche d’attente est annulée; l’appel propage l’arrêt, n’engage pas la pagination suivante et conserve l’intake en pause ainsi que le checkpoint déjà commis. Le helper n’absorbe que l’annulation et les erreurs ordinaires lors du nettoyage; les interruptions système ne sont pas supprimées.

Deux tests déterministes arrêtent la récupération pendant chacun des appels de slot et vérifient l’absence de lecture historique et l’intégrité du checkpoint. Les tests existants couvrent les événements `UNKNOWN`, l’ordre canonique, 429 simulé, interruption et reprise. Aucun changement ne permet d’inventer un ordre ou de promouvoir une réponse provider en preuve indépendante.

La récupération historique consomme toujours un ordre et une finalité annoncés par le provider. Les nouveaux flux sans checkpoint restent soumis à la gate de bootstrap manuel. En cas de désaccord, absence de tuple canonique, erreur RPC, timeout ou résultat incertain, il faut garder `UNKNOWN`/paused; la complétude historique Helius n’est pas prouvée par ces tests offline.

### 24B — SOL/USD

**Aucun changement de code de prix ou de comptabilité n’a été justifié.** Le parseur existant contrôle le feed attendu, les champs numériques, l’intervalle de confiance et le timestamp, mais l’identité du transport et la signature du payload restent non vérifiées. Le validateur de comptabilité n’accepte pas un taux déclaré par le caller; les montants USD continuent à être refusés ou marqués `UNKNOWN`, séparément du ledger `CONFIRMED_CHAIN`.

Une recherche officielle déléguée en lecture seule a comparé deux solutions :

1. **Vérifier localement le payload Hermes signé.** La documentation Hermes sépare le payload binaire à soumettre de la valeur décodée. Les docs Pyth actuelles n’identifient pas de vérificateur cryptographique Python officiel pour ce payload; le client Python public consulté démontre la lecture d’anciens comptes, pas la vérification des mises à jour Hermes ni sa compatibilité avec Pyth Core actuel. La mise à niveau Pyth Core du 26 août 2026 décrit un quorum de signatures mis à jour; un parseur local construit par supposition ne serait pas une preuve.
2. **Lire un compte de prix Pyth sur Solana avec commitment `finalized`.** `getAccountInfo` est une lecture seule et les contrats Pyth vérifient leurs updates on-chain. Cette option pourrait fournir un prix publié sur le compte finalisé après validation de l’adresse, du propriétaire/programme, du feed, du niveau de vérification, de la fraîcheur, du prix et de la confiance. Elle garde une confiance résiduelle au RPC et au bon décodage; le compte courant ne prouve pas automatiquement un taux historique à l’heure d’un ancien événement.

Aucune des deux options n’a été raccordée : le compte/format Pyth Core exact, le décodeur Python compatible, les dépendances disponibles, l’âge maximal et les quotas RPC n’ont pas été validés localement. Les endpoints Hermes/RPC n’ont pas été appelés, aucune clé n’a été créée, aucun paquet ni service payant n’a été activé. L’action sûre pour cette tête est de conserver `UNKNOWN` et l’interdiction de comptabiliser un PnL USD présenté comme validé.

Sources officielles consultées le 2026-11-26 : [Hermes latest price updates](https://docs.pyth.network/api-reference/hermes/latest-price-updates), [Pyth Core upgrade](https://docs.pyth.network/pro/pyth-core-upgrade), [fonctionnement Pyth Pro](https://docs.pyth.network/pro/how-lazer-works), [prix sur Solana](https://docs.pyth.network/pro/use-prices-on-blockchain/solana), [source `PriceUpdateV2`](https://github.com/pyth-network/pyth-crosschain/blob/main/target_chains/solana/pyth_solana_receiver_sdk/src/price_update.rs), [client Python Pyth](https://github.com/pyth-network/pyth-client-py), [RPC Solana `getAccountInfo`](https://solana.com/developers/docs/rpc/http/getaccountinfo), [flux Pyth sponsorisés](https://docs.pyth.network/pro/sponsored-feeds). Ces documents décrivent les contrats/APIs des fournisseurs; ils ne constituent pas une validation locale ni une preuve du fournisseur RPC du déploiement.

### 24C — Crash, transaction et réconciliation PAPER

Les primitives M18 n’ont pas nécessité de modification de production. Deux nouveaux tests dans `tests/test_mission18_paper_admission.py` démontrent :

- Un sous-processus termine un commit de position atomique puis s’arrête avant toute confirmation en mémoire. Après réouverture SQLite, une seule position et une réservation `committed` persistent; le replay du même signal est dédupliqué.
- Une erreur SQLite injectée au milieu de `commit_position` restaure l’ancien état et la révision et laisse la réservation `pending`; aucune position partielle n’est visible.

Un test dans `tests/test_mission24_paper_recovery.py` rouvre une base avec réservation `UNKNOWN`, vérifie qu’elle reste active et que le dashboard read-only la montre `DEGRADED`, avec `signals=UNKNOWN` et sans divulguer les identifiants de signal/corrélation.

La base inbox Helius et la base `bot_state` demeurent séparées; leurs commits ne sont pas atomiques ensemble. Une panne entre les deux requiert une réconciliation manuelle. Une réservation `pending` ou `UNKNOWN` n’est pas libérée ni rejouée automatiquement.

### 24D — Pipeline PAPER

Le code de #35 relie déjà le runtime synthétique aux composants M13/M18/M23 et le dashboard lit des agrégats de réservation. Cette mission n’ajoute pas de constructeur live de `PaperCandidateRequest`: le callback CopyTrade réel n’a pas le paquet de preuves de provenance et ne fournit pas de preuve indépendante de finalité/complétude. Il reste `UNKNOWN`, sans ACK ni admission. L’entrée directe PAPER sans admission M18 et le DCA sans réservation incrémentale restent bloqués. Les tests synthétiques démontrent seulement le code face aux fixtures; ils ne prouvent pas Wallet Intelligence, anti-rug, fraîcheur provider, prix authentique, rentabilité ou fonctionnement opérationnel.

Aucun seuil, plafond d’exposition, sizing, règle de Risk Engine, ou comportement d’arrêt d’urgence n’a été modifié. PAPER reste le seul mode de test; LIVE n’a pas été activé.

## Fichiers modifiés

- `helius_event_store.py` — attente annulable pendant les lectures et délais de reprise.
- `tests/test_helius_event_store.py` — stop pendant lectures de slot processed/finalized.
- `tests/test_mission18_paper_admission.py` — crash après commit et rollback atomique sous erreur SQLite.
- `tests/test_mission24_paper_recovery.py` — persistance/visibilité dashboard d’une réservation `UNKNOWN`.

`sol_usd_provenance.py`, `transaction_accounting.py`, `main.py`, `paper_trader.py`, `mission23_paper_runtime.py`, les seuils/configurations et les routes RPC n’ont pas été modifiés par Mission 24.

## Validation

Python 3.11.9. La suite locale a utilisé `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` pour éviter le plugin web3 global incompatible documenté dans `TESTING.md`, `PYTHON_DOTENV_DISABLED=true`, `TRADING_MODE=PAPER`, `LIVE_EXECUTION_ENABLED=false`, clés provider vides et `SOLANA_RPC_URL=http://127.0.0.1:9` afin qu’aucun test ne contacte de RPC externe.

- `python -m pytest -q` : **933 passed in 30.24s**.
- Suites spécialisées rapportées par leurs propriétaires : Helius/listener **60 passed**; admission/runtime M18/M23 **29 passed**; provenance/comptabilité SOL/USD **91 passed** sans changement de code prix. La revue indépendante a aussi exécuté **76 passed** sur Helius/M18/dashboard et **11 passed** sur M23 runtime.
- `python -m compileall -q .` : réussi.
- `mypy --follow-imports=silent helius_event_store.py mission18_paper_admission.py mission23_paper_runtime.py sol_usd_provenance.py` : `Success: no issues found in 4 source files`.
- `git diff --check` et `git diff --cached --check` : réussis avant commit sur le diff complet, rapport inclus.
- `ruff`, `pip-audit`, `gitleaks` et `coverage` : non installés localement. Ils doivent être vérifiés via GitHub Actions sur le SHA final de cette branche; le CI de #35 ou celui des PR sources ne suffit pas.
- Revue indépendante en lecture seule : aucun finding P0/P1/P2 sur le diff examiné. Elle ne couvre pas l’authenticité d’un provider, le déploiement cible, une opération réelle ou le soak.

La recherche Pyth n’a contacté aucun endpoint Hermes/RPC. Aucun wallet, clé privée, secret, signature, ordre, transaction, endpoint d’exécution, dépense ou base opérationnelle n’a été utilisé. Aucun soak de 24 h, restauration en environnement cible ou audit DEX/CPI indépendant n’est revendiqué.

## Risques et gates

| Priorité | État | Éléments restants |
| --- | --- | --- |
| P0 | Aucun finding sur le diff revu; aucun ordre/secret/RPC d’écriture utilisé | Cela ne vaut pas validation live; accès cible, compte et fournisseur effectifs demeurent non vérifiés. |
| P1 | **Bloqué** — provenance, finalité et exhaustivité Helius ne sont pas vérifiées indépendamment | Bootstrap initial manuel; ordre/finalité provider-reported; aucun certificat de complétude; aucun événement live admis. |
| P1 | **Bloqué** — taux SOL/USD signé/authentifié absent | Signature Hermes non vérifiée; aucun compte on-chain avec décodeur/identité locale validés; PnL USD validé refusé. |
| P1 | **Partiel / manuel** — reprise inter-bases | Les réservations ambiguës restent protégées; un opérateur doit rapprocher inbox Helius et bot-state avant toute résolution. |
| P1 | **Non effectué** — exploitation | Pas de soak PAPER de 24 h, restauration cible neuve ni audit indépendant DEX/CPI. |
| P2 | **Non mesuré** — service et environnement | Quotas/fraîcheur RPC cible, disponibilité Hermes, santé provider persistée, latence réelle et comportement réseau cible restent inconnus. |
| P2 | **Partiel** — outillage local | Ruff, pip-audit, Gitleaks et couverture locale absents; le run CI exact-head doit couvrir ces contrôles. |

## Étapes prioritaires suivantes

1. Maintenir toutes les entrées live et le PnL USD validé bloqués.
2. Avant toute implémentation de prix on-chain, confirmer via le SDK/source Pyth maintenu l’adresse/programme/feed exact et un décodeur compatible; tester owner, feed, niveau de vérification, timestamp, âge, prix et confiance avec des fixtures officielles. Établir quotas/coûts RPC sans endpoint d’exécution.
3. Définir une procédure de bootstrap Helius et de réconciliation inter-bases avec preuves durables; ne pas lever `UNKNOWN` sur silence, timeout ou réponse caller.
4. Quand ces critères techniques seront vérifiables, tester le parcours PAPER réel en environnement cible, puis seulement produire les preuves opérateur de soak 24 h, restauration cible et audit DEX/CPI.

**Conclusion : `PARTIALLY_READY / MANUAL_VERIFICATION_REQUIRED`.** Les tests de reprise et le stop amélioré sont validés localement; la provenance de marché, le taux SOL/USD vérifié et l’intégration PAPER avec données réelles restent bloqués.
