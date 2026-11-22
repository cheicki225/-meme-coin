---
name: token-risk-antirug
description: Audit Solana token safety signals, GoPlus authority and Token-2022 fields, LP data, or the bot's heuristic rug scanner and its filters.
---

# Analyse de risque de tokens et signaux anti-rug

## Déclencheurs

Utiliser cette compétence lorsqu'une tâche porte sur les contrôles de mint/gel/fermeture, Token-2022, hooks ou frais de transfert, données LP/TVL, règles GoPlus, rapports de risque, ou filtres du `RugScanner`. Elle ne certifie pas qu'un token pourra être vendu.

## Audit avant toute modification

- Vérifier branche, état et diff Git; noter les seuils touchés sans lire `.env` ni exposer des valeurs confidentielles.
- Lire `SECURITY.md`, `RISK_MANAGEMENT.md`, `TRADING_GUIDE.md`, `security.py`, `rug_scanner.py`, puis les appels du consommateur dans `main.py` et les réglages associés de `config.py`.
- Examiner le schéma Solana réellement analysé par `_parse_token_security` et les contrôles d'intégrité de `is_valid_solana_security_result`. Ne pas supposer un schéma EVM.
- Lire `tests/test_security.py`, `tests/test_security_reporting.py` et vérifier s'il existe des tests directs du `RugScanner` avant de conclure sur sa couverture.
- Séparer les résultats GoPlus, le score indicatif, le filtre d'achat lorsque celui-ci est activé, et le scanner heuristique de tokens récents; ils n'ont pas la même portée.

## Workflow

1. Établir la provenance, l'heure, la fraîcheur, les champs disponibles et les erreurs de la réponse utilisée. Distinguer `unknown`/indisponible d'un constat rassurant.
2. Pour GoPlus, contrôler les champs Solana présents: autorités mint/freezable/closable, métadonnées, état de compte, non-transferable, transfer fee/hook, DEX TVL et échantillon LP. Un champ absent, incohérent ou mal formé reste inconnu et le résultat incomplet ne doit jamais être affiché en vert.
3. Garder séparés `security_score`, statut de validité et décision d'achat. Le seuil par défaut de TVL et les pénalités LP sont des valeurs configurées/heuristiques; ne pas en déduire que la pool réellement utilisée est protégée.
4. Pour le RugScanner, suivre l'ordre et le coût des filtres réels: créations du développeur, compte de signatures, puis volume de pair; vérifier aussi l'âge, le polling périodique et le stockage du candidat. Le code marque notified=True avant l'appel notifier: cela limite le scanner à une tentative et ne prouve ni la livraison Telegram ni une nouvelle tentative après échec.
5. Ne modifier aucun seuil, score ou condition de blocage sans demande explicite. Ajouter des cas de refus pour données manquantes et adversariales lorsqu'un comportement change.
6. Rédiger les conclusions avec des termes d'indice ou de filtre, jamais avec une promesse anti-rug ou « token sûr ».

## Sécurité

- Ne signer ni diffuser de transaction, ne déclencher aucun achat/vente et ne pas activer LIVE. Garder le contexte PAPER.
- N'appeler aucun fournisseur externe pendant l'analyse, sauf autorisation explicite; privilégier fixtures et mocks.
- Protéger les mint/wallet/signatures dans les rapports et journaux; ne pas exposer clés API ou données brutes du fournisseur.
- Un score positif ne remplace pas la décision de risque ni l'inspection de la route DEX.

## Tests

- Utiliser `tests/test_security.py` pour réponses complètes, autorités actives, contrôles Token-2022, LP vide/incohérent, TVL invalide, token absent et erreurs fournisseur.
- Utiliser `tests/test_security_reporting.py` pour vérifier qu'une donnée inconnue ne produit jamais un affichage rassurant.
- Si les filtres `RugScanner` changent, ajouter des tests isolés de seuil, ordre court-circuité, âge, panne fournisseur et notification idempotente; aucun appel réseau réel.

## Preuves attendues

Présenter les champs réellement observés, le niveau de fraîcheur/provenance disponible, les tests adversariaux exécutés, la logique exacte du seuil/config touché et les inconnues conservées. Marquer les résultats comme fixtures, lecture seule fournisseur, ou exploitation mesurée; aucun test automatisé ne prouve l'absence de rug.

## Limites

Le score GoPlus repose sur un schéma automatisé et les lignes LP retournées peuvent être incomplètes. La TVL maximale rapportée peut concerner une autre pool. `RugScanner` utilise volume, âge, nombre de transactions et historique créateur; ce n'est ni un classificateur de rug avéré ni une preuve de liquidité retirable. Les tests locaux ne mesurent pas les données on-chain du moment.

## Passages de relais

- `wallet-intelligence-collector` pour l'historique du créateur et la qualité des mouvements observés.
- `jupiter-transaction-security` pour le programme/route réellement utilisé par un swap.
- `trading-risk-manager` pour la décision de blocage, exposition ou coupe-circuit.
- `production-readiness-auditor` pour la fraîcheur des sources et les limites de preuve avant déploiement.
