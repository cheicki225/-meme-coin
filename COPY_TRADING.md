# Copy trading

## Flux actuel

`copytrade_listener.py` surveille les wallets configurés en mode buy/sell, déduplique les signatures récentes et classe les transactions à partir des variations token et SOL. Les notifications et l'exécution passent par les callbacks du bot; PAPER reste le mode par défaut. Les ventes LIVE exigent des décimales vérifiées et le wallet d'exécution associé à la position. MultiBuy LIVE est refusé tant que la comptabilité par wallet n'est pas séparée.

## Ce que le signal ne prouve pas

La classification améliorée n'est pas un decodeur complet de toutes les instructions CPI, routes et comptes intermédiaires. Un mouvement non classable ne doit pas être traité comme un achat confirmé. Les notifications WebSocket ne constituent pas un journal durable; la reconnexion et la collecte HTTP doivent combler les trous. Sans Helius configuré, le chemin WebSocket copy trade s'arrête; il n'existe pas encore de remplacement public WebSocket sélectionné.

Le comportement de copie de vente est une règle de stratégie et peut diverger du wallet suivi si sa transaction n'est pas observée ou si le bot subit un retard. Aucun engagement de latence n'est établi.

Voir `copytrade_listener.py`, `main.py`, `paper_trader.py`, `live_trader.py` et [RPC_PUBLIC_LIMITATIONS.md](RPC_PUBLIC_LIMITATIONS.md).
