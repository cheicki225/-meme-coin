# Mission 6 — soak PAPER et méthode de benchmark

## Méthode reproductible

Depuis la racine du dépôt :

```powershell
python scripts/mission6_paper_benchmark.py --cycles 10000 --output docs/benchmarks/mission6-paper-soak.json
```

Le script fixe `TRADING_MODE=PAPER`, désactive l'opt-in LIVE, vide les variables de clés connues dans son processus, crée l'état SQLite dans un `TemporaryDirectory` et ouvre deux serveurs HTTP exclusivement sur `127.0.0.1`. Il n'instancie pas le `main.py` complet, ne lit pas le `.env`, ne contacte aucun fournisseur, et n'ouvre aucun WebSocket externe. Les appels réseau simulés sont uniquement des requêtes JSON-RPC `getHealth`, en lecture seule. La boucle exécute 10 000 dispatchs de créations synthétiques, primitives PAPER d'achat/vente à frais fixes fictifs, sauvegardes SQLite, redémarrages planifiés et retries HTTP.

Le serveur primaire renvoie un 429 une fois pour les identifiants divisibles par 53, 503 systématiquement pour ceux divisibles par 101 et 997. Le fallback local réussit pour les identifiants divisibles par 101 et renvoie 503 pour ceux divisibles par 997. Les délais retry sont accélérés à zéro. Des délais artificiels de 2 ms/4 ms sont ajoutés sur certains événements. Les 24 heures sont une échelle virtuelle équivalente à 10 000 cycles, pas 24 heures de temps réel.

## Résultat exécuté

Run du 22 novembre 2026 sur Windows 10, Python 3.11.9, commit de base `7fe6db45b1d661738a38006bbd8278bfc62b7e06`; le JSON consigne la révision exacte du code. Durée murale : **73,507 secondes**.

| Mesure locale synthétique | n | p50 | p95 | p99 |
|---|---:|---:|---:|---:|
| RPC `getHealth` primaire + fallback, bout en bout | 10 000 | 6,699 ms | 10,357 ms | 19,921 ms |
| Tentatives RPC primaire | 10 405 | 6,516 ms | 9,511 ms | 11,792 ms |
| Tentatives RPC fallback | 129 | 5,996 ms | 9,058 ms | 10,376 ms |
| Dispatch détection token synthétique | 10 000 | 0,029 ms | 0,052 ms | 0,080 ms |
| Primitives PAPER buy/sell | 10 000 | 0,029 ms | 0,052 ms | 0,073 ms |
| Réouverture et lecture SQLite après checkpoints | 19 | 12,239 ms | 14,954 ms | 16,835 ms |

Fautes injectées : **187 réponses 429**, **327 réponses 503 primaires**, **30 réponses 503 fallback**, fallback utilisé **99 fois**, et **10 échecs finaux sur 10 000**. Ces 10 échecs correspondent au scénario local où les deux endpoints sont indisponibles; ce n'est pas un taux d'échec de production.

Frais simulés PAPER : 0,000005 SOL d'entrée + 0,000005 SOL de sortie par cycle, soit **0,05 SOL par côté** au total, convertis à un taux arbitraire de **100 USD/SOL** uniquement pour la fixture. Aucun frais on-chain n'a été payé ou observé.

Le JSON inclut les échantillons agrégés, l'environnement, le commit de base et le hash du diff. La télémétrie interne est une fenêtre bornée de 10 000 événements; les distributions du tableau sont calculées dans le script sur les tableaux d'échantillons complets.

## Interprétation et limites

Ces chiffres sont des latences d'une application Windows locale avec serveur aiohttp loopback et fautes injectées. Ils ne reflètent pas les slots/confirmations Solana, la latence d'un fournisseur RPC, un vrai WebSocket Helius, Jupiter, un DEX, l'inclusion en bloc, le slippage, l'ordonnancement du validateur, MEV ou Telegram. Le dispatch est appelé directement avec des données synthétiques; aucun vrai flux WebSocket n'est ouvert. Ce test ne couvre ni le cycle de stratégie complet ni le rendement.

Ce run ne constitue pas un soak de 24 heures déployé. Il valide une longue boucle synthétique accélérée avec persistance/réouverture dans un environnement isolé. Un soak PAPER opérationnel demande un endpoint sandbox explicitement autorisé, plusieurs jours d'observation du bot complet et des événements anonymisés avec provenance/fraîcheur, sans transaction.

## Benchmark F Project

Le playbook public de F Project affirme « 80% Block 0 », annonce cinq serveurs régionaux sur trois continents, jusqu'à 30 ruggers par wallet, et décrit copy trading, sorties à plusieurs niveaux, Buy The Dip et protection de rugger ([Playbook](https://f-project-1.gitbook.io/f-project-playbook)). Le site commercial indique l'exécution block zero et des wallets de sniping custodial ([site officiel](https://www.fproject.trade/)). Ce sont des déclarations publiques du fournisseur, pas des mesures indépendamment vérifiées.

Les pages consultées ne publient pas les traces brutes, les percentiles p50/p95/p99, le dénominateur et la période derrière le taux 80 %, un taux d'échec défini, des délais de récupération ou le coût réseau effectif par transaction. Les métriques de ce dépôt sont locales et synthétiques : comparaison de performance **non mesurable**. Aucune supériorité de vitesse, fiabilité, sécurité ou rentabilité n'est démontrée.

Un futur benchmark équitable devrait utiliser les mêmes événements on-chain et la même fenêtre, valider les timestamps contre les slots, définir précisément « block 0 », mesurer de la première disponibilité d'événement à l'inclusion finalisée, compter abandons et échecs, publier frais en lamports et coûts prioritaires/DEX sur succès et échec, et répéter sur des cibles comparables.
