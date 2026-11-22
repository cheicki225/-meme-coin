---
name: jupiter-transaction-security
description: Review Jupiter Swap API V2 quotes, unsigned transaction inspection, CPI/DEX allowlisting, fee bounds, confirmation, or ambiguous transaction recovery.
---

# Jupiter et sécurité des transactions

## Déclencheurs

Utiliser cette compétence pour toute modification ou revue du chemin Jupiter V2, de validation de quote ou d'instruction, des CPI/DEX, frais, simulation pré-signature, confirmation et rapprochement des signatures pendantes. Elle couvre les chemins de signature en revue de code uniquement; aucune transaction réelle n'est permise.

## Audit avant toute modification

- Relever le statut Git, la branche et la base PR; inspecter les diffs avant de modifier un fichier.
- Lire `SECURITY.md`, `docs/JUPITER_SECURITY_REVIEW.md`, `jupiter_executor.py`, `wallet.py`, `sqlite_state.py` et les transitions d'état/arrêt d'urgence consommées dans `main.py`.
- Rechercher dans `config.py` les noms `JUPITER_CPI_PROGRAM_ALLOWLIST`, `MAX_SLIPPAGE_BPS`, `MAX_PRICE_IMPACT_PCT`, `MAX_TRANSACTION_FEE_LAMPORTS`, `MAX_JUPITER_RENT_LAMPORTS` et les flags d'exécution; ne pas afficher leurs valeurs ni ouvrir `.env`.
- Cartographier `get_quote`, `_validate_order`, `_validate_unsigned_transaction`, `_simulate_and_validate_jupiter_cpis`, `execute_swap`, l'enregistrement de signature et `/reconcile`. Ne jamais appeler `execute_swap`.
- Examiner `tests/test_jupiter_cpi_audit.py`, `test_jupiter_transaction_validation.py`, `test_jupiter_v2_order_safety.py`, `test_jupiter_redirect_security.py`, `test_jupiter_lookup_safety.py`, `test_jupiter_fee_safety.py`, `test_jupiter_confirmation.py` et `test_jupiter_confirmed_input.py` selon le changement.

## Workflow

1. Décrire le cycle prévu sans exécution: quote → contrôle de l'ordre → construction → inspection non signée → simulation CPI → garde de frais/blockhash → journalisation → confirmation/rapprochement.
2. Comparer l'entrée utilisateur, la quote et les champs réellement encodés dans l'instruction. Vérifier mints, montants exact-in, minimum reçu, payer/taker, signataires, ATA, lookup tables, programmes top-level, blockhash, frais et loyer.
3. Vérifier que seules les routes Metis et instructions Jupiter V6 prises en charge sont acceptées; garder les formats inconnus ou tronqués fail-closed. Les plafonds codés sont 10 % de slippage et 5 % d'impact, même si la configuration demande davantage.
4. Vérifier le contrôle de redirection HTTP sur Order et Execute, et s'assurer qu'aucune clé API ni transaction signée ne suit une redirection.
5. Examiner la simulation sans signature avec trace interne complète; conserver l'allowlist CPI vide par défaut et refuser les programmes externes non explicitement revus. Documenter ce que le simulateur ne vérifie pas.
6. Maintenir l'enregistrement durable avant soumission, l'absence de retry automatique en cas d'issue ambiguë, et l'arrêt jusqu'au rapprochement explicite. Ne jamais reconstruire une position depuis une quote seule.
7. Toute modification de seuil, allowlist ou garde doit être liée à une justification spécifique, des cas de refus et une revue de sécurité indépendante avant toute décision opérationnelle.

## Sécurité

- Ne jamais charger de keypair, signer, appeler `/execute`, soumettre `sendTransaction`, ou diffuser un bundle pendant cette compétence. N'activer aucun opt-in LIVE.
- Garder `TRADING_MODE=PAPER` et `LIVE_EXECUTION_ENABLED=false`; cette vérification ne constitue pas une autorisation d'exécution.
- Ne lire ni copier les secrets. Ne pas inclure clé API, transaction signée, signature, wallet ou réponse RPC brute dans un rapport.
- N'effectuer aucune simulation sur un RPC externe dans ce travail documentaire/code; utiliser des fixtures locales ou des mocks.

## Tests

- Faire les tests unitaires isolés avec transactions et traces synthétiques. Couvrir réussite valide et refus: mints/montants/ATA/programmes erronés, trace CPI absente ou malformée, allowlist vide, frais excessifs, redirection, timeout et signature ambiguë.
- Utiliser les fichiers `tests/test_jupiter_*.py` pertinents, en particulier `test_jupiter_cpi_audit.py`; ne pas confondre le mock `simulateTransaction` avec une simulation RPC réelle.
- Les tests ne doivent jamais importer ou invoquer une voie qui signe ou soumet une transaction réelle.

## Preuves attendues

Fournir le diff, les tests et cas de refus exécutés, les invariants revus dans la séquence, et le niveau de preuve (statique/mocké, lecture seule RPC, ou opération PAPER cible). Indiquer si les CPI/Dex ont été examinés sémantiquement ou seulement comparés à l'allowlist; préciser que la validation d'une transaction ultérieure n'est pas garantie par une simulation.

## Limites

L'allowlist et la simulation ne constituent pas un audit sémantique des programmes DEX ni de leur code déployé. L'état on-chain peut changer après simulation. Le contrôle de débit token exact intervient après confirmation et ne valide pas les tokens à frais de transfert avant le swap. Les tests actuels sont mockés; LIVE demeure non validé.

## Passages de relais

- `trading-risk-manager` pour seuils d'exposition, coupe-circuit et politique de refus.
- `sqlite-recovery` pour intents, signatures pendantes, état durable et rapprochement.
- `python-testing-ci` pour les fixtures transactionnelles, la suite de sécurité et la CI.
- `production-readiness-auditor` pour les critères restant ouverts avant toute préparation opérationnelle.
