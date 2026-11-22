# Gestion du risque

## Garde-fous présents

- Mode PAPER par défaut; LIVE exige `TRADING_MODE=LIVE` et `LIVE_EXECUTION_ENABLED=true`.
- `MAX_OPEN_EXPOSURE_USD`, `MAX_OPEN_POSITIONS` et `CIRCUIT_BREAKER_USD` contrôlent les entrées/exposition et les pertes de session.
- Les entrées réservent l'exposition sous verrou avant les contrôles réseau et sont refusées sur état d'exposition invalide.
- L'arrêt d'urgence est persistant; `/halt` l'active. En LIVE, il bloque tous les swaps via Jupiter, achats **et** ventes. `/resume` ne doit pas libérer un état avec signature pendante non résolue.
- L'arrêt d'urgence n'est pas une liquidation : il ne vend pas les positions. Le suivi local peut continuer, mais les sorties LIVE échoueront tant que l'arrêt est actif. Toute reprise/sortie manuelle exige de réconcilier les signatures pendantes puis de désactiver explicitement l'arrêt.
- Les transferts manuels sont bloqués en PAPER et soumis aux garde-fous LIVE; les bundles Jito à tip séparé restent refusés.
- Les achats au dip et `sell_initials` hérités du simulateur n'altèrent plus les positions en LIVE sans swap confirmé.

## Limites

Les réservations concurrentes sont dans le processus; elles ne constituent pas une coordination multi-instance. La limite de pertes porte sur l'état local/PnL connu et ne garantit pas une borne de perte de marché. Le slippage, les tokens gelés, la faible liquidité, les frais, l'arrêt réseau ou une migration de pool peuvent empêcher une sortie au prix demandé. Comme `/halt` bloque aussi les ventes LIVE, une position peut rester ouverte pendant l'arrêt; aucune liquidation automatique n'est déclenchée. Le LIVE n'est pas prêt.
