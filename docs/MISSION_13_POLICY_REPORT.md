# Mission 13 — Token Selection, Anti-Rug & Trading Strategies

## Statut

**PARTIALLY_READY / MANUAL_VERIFICATION_REQUIRED**

Le cœur de politique est implémenté dans des modules autonomes, déterministes et hors ligne. Il sait refuser une candidature si le score est incomplet, périmé ou incohérent, classer une série de prix fournie, sélectionner une seule stratégie PAPER enregistrée, appliquer les limites de risque déjà configurées et réserver atomiquement l'identifiant/corrélation d'un signal en mémoire.

Le pipeline de Mission 11/12 ne transmet pas encore au même appel une enveloppe canonique de signaux horodatés, les séries de marché et les identifiants de déduplication persistés. L'adaptateur de Mission 13 n'est donc pas encore appelé par main.py ou paper_trader.py. Aucun trade PAPER opérationnel n'a été simulé pendant cette mission.

Les limites d'exposition, de positions et de pertes sont des entrées obligatoires du futur callsite. La fonction les transmet aux contrôles existants, mais ne lit pas config.py elle-même; le callsite doit donc fournir les valeurs configurées inchangées. Le snapshot des réservations doit être pris sous le verrou PAPER existant. Sans ces garanties d'intégration, ce module autonome ne doit pas être utilisé comme admission opérationnelle.

## Base et dépendances vérifiées le 24 novembre 2026

- La branche de Mission 13 est créée depuis la tête observée de la PR #21, branche codex/mission-10-autonomous-execution-integration-2026-11-24, commit 0cc54f77312d6fbf89e0f99c191f840bbddaf985. La PR #21 reste ouverte, brouillon et non fusionnée.
- La PR #22 (Mission 11) est une branche sœur sur #21, tête observée 9590196c40a64ef478a4d4e7c44ade5dee105f12. Son diff touche main.py et paper_trader.py.
- La PR #23 (Mission 12) est aussi une branche sœur sur #21, tête observée ba11a28c90f071a678a8d5b1c51b612b203f2988. Elle touche helius_event_store.py, ses tests et sa matrice de capacités.
  - Sa CI exacte sur cette tête est verte (run 45501875324 : 630 tests, Ruff, pip-audit, Gitleaks et couverture 39,30 % combinée ligne+branche).
- La PR #16 (provenance anti-rug) est ouverte sur l'ancienne base #8.6. La PR #21 a repris son delta dans l'intégration Mission 10; le code de rug_scanner.py sur la base exacte de #21 contient déjà l'exigence de timestamp Helius enrichi et distingue le résultat inconnu d'un passage.
- Le checkout main local et le clone de Mission 11 avaient des changements locaux préexistants. Aucun n'a servi de checkout de travail; seul le clone isolé Mission 13 a été modifié.

Cette PR est une branche sœur de #22 et #23, basée sur leur base commune #21. Elle ne les fusionne pas, ne les réécrit pas et ne modifie pas leurs fichiers.

## Changements

- mission13_scoring.py fournit SignalObservation avec source, provenance et timestamp aware, SignalRule et ScoringProfile configurables, un score pondéré explicable et les contributions par signal. Aucun poids, seuil ou profil de marché réel n'est inventé. Tout signal requis absent, dupliqué de façon conflictuelle, mal formé, non fini, hors domaine, futur, naïf, périmé ou sans provenance bloque le score. Les règles configurées peuvent aussi refuser explicitement un indicateur de risque.
- mission13_market_policy.py classe les observations de clôture fournies en tendance haussière, tendance baissière, latéralité, volatilité élevée ou UNKNOWN. La fonction n'utilise que les points dans l'ordre et à l'heure du signal, refuse les gaps et les observations périmées/futures, et expose les métriques descriptives. La sélection choisit au plus une stratégie PAPER parmi les identifiants et exigences explicitement enregistrés.
- mission13_admission.py compose le score, le régime, la stratégie, le mode PAPER, les limites de position/exposition existantes de risk_controls.py, la limite de pertes fournie par le bot et un registre de signaux atomique en mémoire. Les valeurs de limite doivent venir du callsite configuré inchangées; l'adaptateur ne les récupère pas lui-même. Un signal dupliqué ou un groupe de corrélation déjà actif est refusé.
- tests/fixtures/mission13_synthetic_paper_candidate.json est un jeu de données entièrement synthétique, créé pour le test d'intégration de politique. Il ne provient ni de Solana, ni d'un fournisseur, ni d'une collecte réelle. Son score attendu de 85 et son régime haussier servent uniquement à vérifier le calcul et la composition.
- tests/test_mission13_scoring.py, tests/test_mission13_market_policy.py et tests/test_mission13_admission.py couvrent les calculs explicables, les refus, le mode PAPER, la classification descriptive, les limites et la déduplication.

Aucun fichier existant de main.py, paper_trader.py, config.py, risk_controls.py, rug_scanner.py, security.py ou helius_event_store.py n'est modifié.

## Contrat d'intégration PAPER avec les Missions 11/12

Avant d'appeler evaluate_paper_candidate, le consommateur devra :

1. Construire les observations depuis les sorties des adaptateurs de données déjà autorisés. Chaque entrée doit avoir un nom de signal stable, une valeur au type/unité attendu, un timestamp source vérifiable et les identifiants de source/provenance sans clé, URL secrète ou payload brut.
2. Fournir un profil versionné explicitement revu; le profil doit marquer les champs essentiels comme requis et donner poids, domaine, fraîcheur maximale et éventuelle règle de refus. Aucun profil par défaut de cette PR ne permet d'acheter.
3. Fournir une série historique dont le dernier point est au plus à as_of, ordonnée, sans trou supérieur à la politique configurée. Une série de prix ponctuelle ne suffit pas à inférer un régime.
4. Enregistrer des StrategyCandidate avec les identifiants, régimes compatibles et signaux requis. Les types sniping_existing et copy_trading_existing ne font que référencer des sources de signaux déjà présentes; ils ne créent ni signal de sniping/copie ni trade.
5. Fournir explicitement les limites configurées actuelles du bot sans les augmenter, ainsi que l'état du portefeuille. Prendre le snapshot des réservations sous le verrou PAPER existant et effectuer l'admission dans la même section critique.
6. Persister les paires signal_id/correlation_key avec un état committed/pending avant de reprendre après redémarrage. Ne confirmer la réservation de PaperSignalLedger qu'après persistance de la décision PAPER; la libérer si aucune position PAPER n'est créée. Le registre de cette PR est mémoire seulement.

La fonction refuse tout mode autre que PAPER. L'adaptateur ne signe, ne diffuse et ne construit aucune transaction. Son registre local n'est pas durable et ne protège pas à lui seul contre un rejeu après redémarrage ou entre plusieurs instances.

## Anti-rug et fraude : portée réelle

Le contrôle GoPlus Solana existant distingue déjà les autorités mint/gel/fermeture, les risques Token-2022, le TVL déclaré et l'échantillon de détenteurs LP. Le RugScanner existant filtre les créations du développeur, le nombre de signatures, l'âge du candidat et le volume rapporté. Ces contrôles restent heuristiques; les horodatages fournisseur, la couverture complète des LP, la liquidité retirable et les programmes CPI/DEX ne sont pas prouvés.

Le nouveau score permet à un profil explicite de rendre bloquants des signaux documentés tels qu'une autorité active ou une variation de liquidité. Il ne calcule pas ces indicateurs on-chain et ne détecte pas, sans données d'entrée, le wash trading, les bundles, le contrôle coordonné de portefeuilles ou une intention frauduleuse. Un passage du score ne signifie jamais qu'un token est sûr ou vendable.

## Validation et interprétation

- Tests Mission 13 ciblés : `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `PYTHON_DOTENV_DISABLED=true`, puis `python -m pytest -q tests/test_mission13_scoring.py tests/test_mission13_market_policy.py tests/test_mission13_admission.py` — **48 passés**.
- Suite locale complète sur la base de #21 et la branche Mission 13 : `python -m compileall -q .` — réussi; `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `PYTHON_DOTENV_DISABLED=true`, puis `python -m pytest -q` — **670 passés**. Le contournement du plugin pytest global est documenté par TESTING.md.
- Les exécutables Ruff, pip-audit et Gitleaks ne sont pas installés dans l'environnement local. La CI exacte de la PR doit encore valider compilation, PAPER par défaut, suite pytest avec couverture lignes/branches, Ruff, pip-audit, Gitleaks et l'artefact couverture.
- Revue indépendante en lecture seule : **acceptable comme PR brouillon du noyau de politiques**, non comme admission opérationnelle. Elle a confirmé que les valeurs de risques restent fournies par l'appelant, que le snapshot exposition/réservations doit être pris sous le verrou PAPER, que le registre est volatile et que les timestamps/provenance déclarés ne sont pas authentifiés par le module.

La suite ciblée Mission 13 utilise des observations et prix synthétiques. Elle prouve la logique de refus et la composition déterministes, pas la qualité du score, un avantage, un PnL ni l'absence de rug. Aucun modèle entraîné n'a été ajouté. Aucun dataset réel vérifié n'est présent; aucune métrique hors-échantillon ou comparaison de rendement n'est donc annoncée. Les timestamps sont validés par rapport aux valeurs fournies, mais leur correspondance aux horodatages réels fournisseur/on-chain doit être garantie par l'adaptateur source.

Aucun backtest financier n'est revendiqué pour cette PR. Une validation comparative future devra conserver le dataset et son manifeste, séparer chronologiquement entraînement/calibration et test hors-échantillon si un modèle est introduit, fournir les coûts Solana/Jupiter et hypothèses de slippage, préserver le mapping signal → bougie suivante et contrôler le lookahead.

## Backlog priorisé

1. **P0 — Contrat de données fiable** : faire fournir par les Missions 11/12 des signaux de sélection unifiés avec timestamp provider/on-chain, provenance, unités, état UNKNOWN et âge mesurable. Maintenir le refus lorsque l'une de ces preuves manque.
2. **P0 — Limites et réservation** : relier les valeurs de paramètres configurées sans les augmenter, obtenir l'exposition/positions réservées sous le verrou PAPER, et tester deux signaux concurrents jusqu'à la persistance de la position.
3. **P0 — Branchement PAPER** : intégrer l'adaptateur à l'admission PAPER seulement après stabilisation des callsites #22/#23, revue du diff combiné et tests sur leurs deux chemins (track_creation et track_buy/copy). Garder le mode LIVE explicitement refusé.
4. **P0 — Déduplication durable** : relier le registre à une persistance idempotente et aux réservations concurrentes existantes; tester course, crash après réservation, reprise et rejeu.
5. **P1 — Indicateurs anti-manipulation** : obtenir des sources vérifiables pour la concentration des détenteurs, retraits rapides de liquidité, activité de création/vente du développeur et activité coordonnée; définir/calibrer les seuils sur un dataset vérifiable, conserver les inconnues comme refus.
6. **P1 — Évaluation de stratégies** : intégrer la sélection au replay OHLC quand un historique authentifié est fourni; mesurer les métriques hors-échantillon avec frais, slippage, gaps et scénarios conservateurs, sans sélectionner sur les résultats du test.
7. **P1 — Gates opérationnels existants** : soak PAPER cible de 24 h, restauration d'état, et audit DEX/CPI indépendant restent des prérequis du projet; cette PR ne les effectue pas.

## Retour arrière

Les nouveaux fichiers sont isolés sur la branche dédiée Mission 13. Supprimer cette branche non fusionnée ou fermer la PR annule ce delta sans modifier #21, #22, #23, #16 ou le checkout main local.
