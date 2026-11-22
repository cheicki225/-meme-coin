# Préparation au mode LIVE

**Statut actuel : BLOCKED — ne pas utiliser de fonds réels.** Le chemin de code existe, mais la disponibilité technique ne constitue pas une approbation d'exploitation.

| Contrôle | Statut | Éléments manquants |
|---|---|---|
| Mode PAPER par défaut | VERIFIED | `TRADING_MODE` normalisé et limité à PAPER/LIVE |
| Double opt-in LIVE | TESTED | Aucun usage réel ne doit être déclenché pendant cette phase |
| Jupiter V2 et validation locale | TESTED | Pas de test transactionnel mainnet signé ni de simulation adversariale du service réel |
| Montants/frais/PnL confirmés | TESTED | Cours USD au moment de la transaction; pas de valorisation historique stable |
| Reprise des signatures ambiguës | TESTED | Réconciliation encore manuelle si le statut on-chain reste indéterminé |
| Contrôles d'exposition et arrêt d'urgence | TESTED | En LIVE, l'arrêt bloque tous les swaps, ventes comprises, sans liquidation; pas de soak PAPER ni de preuve sur l'environnement cible |
| RPC et WebSocket | BLOCKED | Fournisseur/quotas déployés inconnus; capacité dégradée sans Helius |
| Revue de la transaction | IN PROGRESS | Instructions CPI/DEX et composants externes à auditer indépendamment |
| Tests de sécurité des dépendances et secrets | IN PROGRESS | CI de la nouvelle révision à terminer et archiver |
| Procédure d'exploitation/restauration | NOT STARTED | Exercice documenté sur la base réelle de PAPER et alertes de déploiement |

## Conditions minimales avant toute décision LIVE

1. Obtenir une CI entièrement verte sur le commit final (compilation, tests, Ruff, pip-audit, Gitleaks).
2. Choisir et vérifier un RPC dédié, son endpoint de secours, ses limites et la fraîcheur des réponses.
3. Exécuter un soak PAPER assez long pour couvrir les déconnexions, rate limits, redémarrages, sauvegarde/restauration et alertes Telegram.
4. Faire relire indépendamment les transactions et programmes CPI/DEX autorisés.
5. Réconcilier toutes les signatures pendantes et positions existantes; ne jamais reprendre après une issue ambiguë.
6. Documenter le plan de perte maximale, fonds de test séparés, clés, rollback et intervention humaine.

Aucune transaction réelle n'a été exécutée pendant ces travaux. Le mode LIVE est volontairement non prêt.
