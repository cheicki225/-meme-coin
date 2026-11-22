---
name: trading-risk-manager
description: "Use when auditing or changing position admission, exposure limits, loss breakers, emergency-stop behavior, or resume protections."
---

# Gestion du risque

## Déclencheurs

Active ce Skill pour examiner `risk_controls.py`, le sizing/exposure admission, les réservations concurrentes, les pertes de session, `/halt` ou `/resume`, et le coupe-circuit. Pour une question sans impact sur les protections, utilise le Skill du domaine concerné.

## Audit avant toute modification

- Vérifie branche, stack de PR et statut Git; inspecte le diff ciblé avant tout changement.
- Lis `RISK_MANAGEMENT.md`, `SECURITY.md`, `CONFIGURATION.md`, puis `risk_controls.py`, les appels dans `paper_trader.py`/`live_trader.py`, l'état et les transitions dans `monitoring_list.py`, ainsi que `tests/test_risk_controls.py` et `tests/test_emergency_stop_persistence.py`.
- Identifie les unités, la source des valeurs de configuration, les positions ouvertes, les réservations, les chemins d'échec et le comportement si la persistance échoue.
- Reprends les limites actuellement configurées; n'en invente aucune valeur universelle et ne déduis pas une borne de perte garantie.

## Workflow

1. Établis les invariants d'admission: état d'arrêt valide, compteurs de pertes finis/non négatifs, limites positives, état des positions fiable, et inclusion des réservations.
2. Suis le cycle d'arrêt: activation avant sauvegarde, maintien de l'arrêt en mémoire si l'écriture échoue, refus de reprise avec registre de transactions pendant ou mal formé.
3. Analyse si `/halt` arrête les nouvelles transactions ou entraîne d'autres effets: l'arrêt d'urgence n'est pas une liquidation et peut bloquer aussi les ventes LIVE.
4. Propose un changement ciblé avec cas de refus couvrant les valeurs mal formées, les limites atteintes, les courses d'admission et les échecs de stockage.
5. Obtiens une autorisation explicite avant toute modification de limite, sizing ou protection; ne désactive ni n'assouplis un refus pour faire passer un test.

## Sécurité

PAPER demeure le mode par défaut. N'active pas LIVE, ne signe ni ne diffuse de transaction et ne manipule aucune clé. Une modification des limites, de la règle de sizing ou du comportement de l'arrêt exige une demande explicite de l'utilisateur. Les tests restent déterministes et n'appellent aucun RPC de transaction.

## Tests

Couvre `tests/test_risk_controls.py`, `tests/test_emergency_stop_persistence.py`, `tests/test_live_inherited_paper_guards.py`, `tests/test_manual_transfer_safety.py` et `tests/test_telegram_risk_status.py` selon le changement. Ajoute les cas de refus avant les cas positifs. Vérifie qu'un état corrompu ne devient jamais permissif et que l'échec de sauvegarde ne retire pas l'arrêt.

## Preuves attendues

Présente les règles exactes exercées, les valeurs de fixtures (avec unités), les refus attendus et l'état persistant après échec. Distingue les tests simulés, le comportement PAPER et une validation du déploiement. Ne présente pas `CIRCUIT_BREAKER_USD` comme plafond de perte réalisé: le compteur dépend de l'état local connu et des pertes comptabilisées.

## Limites

Les réservations d'exposition sont locales au processus, sans coordination multi-instance. Les pertes de marché peuvent dépasser le seuil local; liquidité, slippage, panne RPC ou blocage des ventes peuvent empêcher une sortie. L'arrêt d'urgence ne liquide pas les positions. LIVE demeure non validé.

## Passages de relais

- Unités, coût et PnL des trades: `pnl-accounting`.
- Signatures ambiguës, stockage et reprise: `sqlite-recovery`.
- Effets Telegram de `/halt` et états opérateur: `telegram-monitoring`.
- Entrées copiées: `copy-trading`; MultiBuy/MultiSell par compte: `multi-wallet-execution`.