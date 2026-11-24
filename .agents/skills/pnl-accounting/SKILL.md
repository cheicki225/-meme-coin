---
name: pnl-accounting
description: "Use when reviewing or changing trade cash flows, token deltas, fees, cost basis, realized PnL, or accounting displays."
---

# PnL et comptabilité

## Déclencheurs

Active ce Skill pour PnL réalisé/non réalisé, frais, cash flows SOL, coût de base, comptabilisation des buys/sells, rapprochement des frais échoués ou affichage de résultats. Pour les métriques historiques d'une stratégie, utilise `backtesting-validation`.

## Audit avant toute modification

- Vérifie le statut Git et le point de départ empilé; lis `SECURITY.md`, `RISK_MANAGEMENT.md`, `BACKTESTING.md`, `transaction_accounting.py`, les helpers de frais/PnL dans `paper_trader.py`, `live_trader.py`, `execution_recovery.py` et `monitoring_list.py`.
- Trace les unités, le sens des deltas, le payeur des frais, la conversion SOL/USD, la persistence des positions/trades, et le moment où une transaction devient « accounted ».
- Inspecte `tests/test_transaction_accounting.py`, `tests/test_paper_trader_cashflow.py` et `tests/test_execution_recovery.py` avant de toucher au calcul.

## Workflow

1. Sépare PAPER (prix/frais simulés) de LIVE (métadonnées de transaction confirmée). Ne fusionne pas les deux sources dans une statistique sans libellé et provenance.
2. En LIVE, pars des deltas confirmés du wallet et conserve les montants de tokens en unités entières jusqu'à des décimales vérifiées. Le delta SOL du payeur inclut les frais réseau et le loyer reflétés dans ses soldes.
3. Refuse une comptabilité lorsque l'état on-chain, le mint, les décimales, le débit exact-in ou le delta SOL sont incomplets/incohérents; conserve la transaction en attente de rapprochement.
4. Préserve l'historique: si d'anciens frais ou taux sont inconnus, marque le PnL incomplet au lieu de réécrire silencieusement les totaux.
5. Ajoute des fixtures adversariales (frais manquants, booléens, float, delta de mint inattendu, transaction échouée, wallet/payeur incorrect) pour tout comportement modifié.

## Sécurité

Ne signe, ne soumets, ne retransmets et ne réconcilie pas une transaction via un compte réel pendant l'analyse. Pas de clés, tokens, signatures réelles, soldes ou réponses RPC brutes dans les logs/rapports. Aucune activation LIVE. Tout changement qui influence les limites ou le coupe-circuit passe par `trading-risk-manager` et nécessite autorisation si une protection change.

## Tests

Exécute les fixtures locales de `tests/test_transaction_accounting.py`, `tests/test_paper_trader_cashflow.py` et `tests/test_execution_recovery.py`; ajoute des tests de refus pour données manquantes, arrondis, deltas token non liés et conversion manquante. Les tests doivent rester réseau-faux, répétables et distincts d'une comptabilisation en PAPER ou d'un trade réel.

## Preuves attendues

Pour chaque résultat, fournis la source, la devise/unité, le calcul brut→net, les frais inclus ou inconnus, le taux USD et son horodatage s'il existe, ainsi que l'état confirmé/en attente. Indique explicitement si l'information est simulée ou issue de fixtures RPC. Un calcul exact sur fixture ne valide pas l'historique du bot.

## Limites

Le SOL/USD employé par LIVE est le cours récupéré au moment du trade; l'historique de conversion n'est pas reconstruit. Les frais des positions historiques peuvent être incomplets. Les tokens à frais de transfert restent bloqués faute de modèle vérifié. Une signature réussie mais sans position durable nécessite une réconciliation manuelle.

## Passages de relais

- Reprise de transaction ou état SQLite: `sqlite-recovery`.
- Admission, réservations et seuil de pertes: `trading-risk-manager`.
- Positions réparties entre wallets: `multi-wallet-execution`.
- Coûts et drawdown du replay: `backtesting-validation`.
- Présentation Telegram: `telegram-monitoring`.