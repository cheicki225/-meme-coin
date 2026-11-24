---
name: copy-trading
description: "Use when reviewing or changing watched-wallet buy/sell detection, WebSocket subscriptions, signature deduplication, or copy-trade callbacks."
---

# Copy trading

## Déclencheurs

Active ce Skill pour auditer, corriger ou étendre la détection des achats/ventes de wallets suivis, les callbacks de copy trade, les modes `track_buy`, `track_sell` et `buy_on_dev_sell`, ou leur chemin vers les positions. Pour l'historique et le classement général de wallets sans copie, passe à `wallet-intelligence-collector`.

## Audit avant toute modification

- Vérifie la branche, le graphe des PR et les changements locaux avant d'éditer; conserve les modifications existantes et travaille hors du checkout `main`.
- Lis `COPY_TRADING.md`, `RPC_PUBLIC_LIMITATIONS.md`, puis `copytrade_listener.py`, le raccordement dans `main.py`, les contrôles de `PaperTrader.open_position` et les tests `tests/test_copytrade_listener.py`.
- Trace une signature depuis la notification jusqu'au callback et à l'admission d'entrée. Sépare les modes de copie des wallets `track_creation` et des alertes de transfert.
- Vérifie comment les signatures en cours et déjà vues sont conservées, et si une panne de fetch permet une nouvelle tentative. Ne présume pas qu'une notification WebSocket est un journal durable.

## Workflow

1. Décris le contrat d'événement observé (wallet source, mint, direction, signature et données réellement disponibles) et les branches qui ignorent ou refusent un événement.
2. Pour une correction, conserve le traitement fail-closed des mouvements ambigus et la déduplication pendant les fetchs concurrents. N'altère les réglages utilisateur qu'avec un périmètre explicite.
3. Vérifie les chemins de reconnexion et les trous de collecte séparément de la classification d'une transaction; signale les pertes d'événements possibles.
4. Ajoute une régression hors ligne avec RPC et callbacks simulés quand le comportement change; ne lance pas le bot ni un flux réseau pour valider une classification.

## Sécurité

Les transactions observées sont des signaux, pas une preuve universelle d'achat ou de vente: le parseur ne décode pas toutes les routes ni tous les CPI. N'envoie ni ne signe de transaction et ne transforme jamais une activité incertaine en achat confirmé. Garde PAPER par défaut, les RPC publics actuels et les secrets hors du code, des logs et des rapports. Aucun changement n'autorise LIVE.

## Tests

Utilise les fixtures locales de `tests/test_copytrade_listener.py` pour l'échec transitoire et les notifications concurrentes. Ajoute des cas pour événements mal formés, mauvaise classification, redelivery et callback non appelé lorsque la preuve manque. Les tests ne doivent ni joindre Helius ni un autre RPC. Consulte `TESTING.md` pour les commandes et l'isolement de plugins locaux.

## Preuves attendues

Rapporte la signature fictive/fixture et le chemin testé, le nombre de callbacks observés, le traitement d'un doublon et celui d'une réponse invalide. Distingue un test simulé d'un flux WebSocket PAPER réellement observé; ne prétends pas mesurer la latence de copie avec des fixtures.

## Limites

`copytrade_listener.py` dépend du WebSocket Helius configuré pour ce flux et s'arrête sans lui; aucun remplacement public WebSocket n'est sélectionné. Les notifications peuvent être perdues lors d'une déconnexion. La classification des variations de soldes est heuristique et la copie de vente peut diverger si l'événement source manque ou arrive tard.

## Passages de relais

- Événements, signatures ou historique de wallet: `wallet-intelligence-collector`.
- Connexion, limites, retries et mesures RPC: `solana-rpc-performance`.
- Évaluation du token avant entrée: `token-risk-antirug`.
- Exposition, admission ou arrêt d'urgence: `trading-risk-manager`.
- Positions exécutées par wallet et réconciliation: `multi-wallet-execution` et `pnl-accounting`.