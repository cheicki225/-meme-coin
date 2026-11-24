---
name: multi-wallet-execution
description: "Use when auditing managed wallets, per-wallet position accounting, or the MultiBuy/MultiSell execution and reconciliation paths."
---

# Exécution multi-wallet

## Déclencheurs

Active ce Skill pour `wallet_manager.py`, les wallets gérés, le choix du wallet d'exécution, MultiBuy/MultiSell, ou l'attribution et la vente d'une position détenue par plusieurs wallets. Pour un swap Jupiter simple, utilise `jupiter-transaction-security`.

## Audit avant toute modification

- Inspecte d'abord la branche, les PR empilées et les changements locaux; garde `main` intact.
- Lis `CONFIGURATION.md`, `SECURITY.md`, `wallet_manager.py`, `live_trader.py`, `paper_trader.py`, les callbacks de `telegram_bot.py`, `monitoring_list.py`, `transaction_accounting.py` et les tests de transfert, de garde-fous et de comptabilité.
- Confirme pour chaque opération quel wallet signe, quelle position porte `execution_wallet`, comment un échec/une signature en attente est réconcilié et où les résultats sont persistés.
- Vérifie ce qui existe réellement: `multibuy_enabled` est désactivé par défaut; le chemin LIVE refuse MultiBuy tant que les positions par wallet ne sont pas réconciliées. N'infère pas l'existence de MultiSell.

## Workflow

1. Distingue gestion de plusieurs wallets, sélection d'un wallet actif et exécution sur plusieurs wallets: ce sont des capacités différentes.
2. Pour une proposition MultiBuy/MultiSell, définis une position indépendante par wallet: mint, unités brutes/décimales validées, coût, frais, signature, état d'exécution, sortie et récupération.
3. Trace la comptabilité et l'arrêt d'urgence à travers chaque sous-opération; un résultat partiel ne doit pas être présenté comme un panier exécuté avec succès.
4. Exige des tests simulés pour succès partiel, timeout ambigu, refus d'un wallet, doublon et reprise avant tout changement comportemental. Signale l'absence de couverture dédiée au lieu de supposer le comportement correct.

## Sécurité

Ne lis, n'imprimes, ne transmets et ne journalise jamais de clé privée ou de secret de chiffrement. Les wallets gérés sont persistés chiffrés avec `WALLET_ENCRYPTION_KEY`; la clé privée peut apparaître une fois lors de création/import et un secret envoyé par Telegram peut persister dans son historique. N'utilise aucun secret de l'environnement. PAPER reste par défaut; pas de signature, transfert, achat ou vente réel; aucune activation LIVE. Ne retire pas le refus MultiBuy LIVE.

## Tests

Réutilise les tests isolés `tests/test_live_inherited_paper_guards.py`, `tests/test_manual_transfer_safety.py`, `tests/test_transaction_accounting.py` et `tests/test_execution_recovery.py` quand ils couvrent le chemin touché. Les futures primitives multi-wallet nécessitent des tests propres à l'attribution par wallet, à la reprise après échec partiel et à l'absence d'utilisation d'un wallet de repli erroné. N'utilise que des keypairs/fixtures synthétiques hors réseau.

## Preuves attendues

Documente la correspondance position→wallet public (sans clé), les montants en unités brutes puis décimales vérifiées, l'état de chaque sous-opération et le traitement d'un résultat ambigu. Sépare une UI de configuration, un test de simulation PAPER et une exécution disponible. Ne déclare pas MultiBuy/MultiSell opérationnel si seul le formulaire Telegram ou le flag existe.

## Limites

Le dépôt fournit le gestionnaire de wallets chiffrés et la sélection d'un wallet actif; la comptabilité indépendante nécessaire aux positions LIVE MultiBuy est explicitement manquante et LIVE MultiBuy est refusé. Aucun chemin MultiSell indépendant par wallet n'est établi par le code inspecté. Les tests listés ne forment pas une validation multi-wallet complète.

## Passages de relais

- Swap, route, signature et confirmation Jupiter: `jupiter-transaction-security`.
- Réservation globale et coupe-circuit: `trading-risk-manager`.
- Cash flows et PnL par wallet: `pnl-accounting`.
- Transactions en attente et base de données: `sqlite-recovery`.
- Réglages Telegram/permissions d'administration: `telegram-monitoring`.