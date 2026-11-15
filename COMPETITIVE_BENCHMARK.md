# Benchmark fonctionnel (documentation publique)

Relevé au 15 novembre 2026 à partir de la documentation officielle des produits. Ce tableau compare des fonctions annoncées, pas la vitesse réelle, la rentabilité, la sécurité indépendante ou les tarifs du jour. Aucun test d'exécution concurrent n'a été fait.

| Domaine | Bot de ce dépôt | Produits documentés |
|---|---|---|
| Copie de wallets | Listener WebSocket et filtres configurables; dépend des services Helius et d'une classification locale; tests mockés, latence en production non mesurée | [Trojan](https://docs.trojan.com/terminal-overview/tools-and-widgets/copy-trading-bot) documente copy-buy, copy-sell, auto-sell TP/SL et contrôles de frais/slippage/MEV. [BONKbot Telemetry](https://docs.bonkbot.io/telemetry/insights/wallet-tracker) documente suivi et visualisation des trades de wallets; ceci ne prouve pas une copie automatique dans la documentation consultée |
| Sorties automatisées | TP/SL et ventes par paliers pilotés par le processus du bot; la surveillance nécessite que le processus et RPC restent disponibles; stop loss PAPER local | [BONKbot Auto Strat](https://docs.bonkbot.io/telemetry/trading/auto-strat) documente des stratégies réutilisables, TP empilés, SL fixes/trailing et application à l'achat. Trojan décrit aussi Auto Sell et Copy Sell |
| Exécution | Jupiter Swap V2 via `/order` et `/execute`; validation locale et signature persistée; LIVE toujours non validé | [Jupiter Swap API](https://developers.jup.ag/docs/api-reference/swap/order) et [Execute](https://developers.jup.ag/docs/api-reference/swap/execute) fournissent l'infrastructure API, pas un comparatif de performance. [Axiom](https://docs.axiom.trade/axiom/swap/market) documente un terminal de swap/portfolio et des données de trade |
| Risque et transparence | PAPER par défaut, kill switch persistant, limites globales d'exposition et réconciliation des signatures; aucune revue indépendante complète | Les documentations de produit mettent en avant TP/SL, filtres, portefeuille et frais/MEV; elles ne fournissent pas de preuve que ce bot serait plus sûr ou plus rentable |
| Backtesting | Replay OHLC déterministe disponible, mais pas intégré à la décision de suivi complète; coûts et données restent partiels | Les pages consultées décrivent surtout trading/monitoring live. Aucun backtest comparable de performance historique n'est publiquement vérifié ici |

## Écart prioritaire à combler

Les produits hébergés présentent stratégies appliquées à l'achat, ordres empilés, copie de ventes et filtres portefeuille comme fonctions intégrées. Ce dépôt a des mécanismes similaires en code, mais ne dispose ni d'une exploitation hébergée vérifiée, ni d'un niveau de service, ni de mesures de latence, ni d'un audit indépendant. L'effort prioritaire est la fiabilité démontrable, la mesure de frais/PnL et l'intégration d'un backtest sans biais, avant toute promesse de vitesse ou d'avantage compétitif.

Sources officielles vérifiées : [Trojan Copy Trading](https://docs.trojan.com/terminal-overview/tools-and-widgets/copy-trading-bot), [BONKbot Auto Strat](https://docs.bonkbot.io/telemetry/trading/auto-strat), [BONKbot Wallet Tracker](https://docs.bonkbot.io/telemetry/insights/wallet-tracker), [Axiom Market](https://docs.axiom.trade/axiom/swap/market), [Jupiter Order](https://developers.jup.ag/docs/api-reference/swap/order).
