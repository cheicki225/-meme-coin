# Codex Playbook — Mise à niveau du bot de trading Solana

## Dépôt et branche de travail
- Dépôt : https://github.com/cheicki225/-meme-coin
- Branche de sécurité existante : `improvements/safety-observability`
- PR de référence : https://github.com/cheicki225/-meme-coin/pull/1

## Objectif
Transformer le bot en système de trading Solana robuste, observable et fail-closed, sans activer automatiquement le trading réel.

## Travail obligatoire

### 1. Audit initial
- Lire tout le dépôt avant de modifier le code.
- Cartographier les flux : détection Pump.fun → scoring → décision → construction de transaction → signature → broadcast → suivi de position.
- Identifier les chemins PAPER, DRY-RUN et LIVE.
- Rechercher les appels d’exécution, les secrets, les clés privées, les retries et les tâches asynchrones.

### 2. Sécurité et garde-fous
- Intégrer `safety_guard.py` dans tous les chemins d’achat et de vente pertinents, y compris les achats multiples.
- Exécuter `safety_runtime.py` au démarrage et avant toute action sensible.
- Refuser par défaut toute configuration ambiguë ou invalide.
- Imposer : limite par position, perte journalière maximale, nombre maximal de positions, réserve SOL, emergency stop et clé d’idempotence obligatoire.
- Interdire tout passage automatique en LIVE. LIVE doit nécessiter une activation explicite et vérifiable.
- Ne jamais afficher, logger ou committer une clé privée, seed phrase ou secret.

### 3. Idempotence et fiabilité
- Créer une clé d’idempotence stable par intention de trade.
- Empêcher les doubles achats/ventes après retry, reconnexion WebSocket ou événement dupliqué.
- Ajouter des timeouts, retries bornés avec backoff et gestion claire des erreurs.
- Gérer les blockhash expirés, les transactions échouées et les confirmations incomplètes.
- Préserver la cohérence de l’état en cas de redémarrage.

### 4. Détection et exécution Solana
- Conserver la détection Pump.fun existante mais isoler les adaptateurs réseau.
- Vérifier les décimales, balances, slippage, frais et rentabilité minimale avant signature.
- Séparer clairement : signal, risk check, transaction build, signature et broadcast.
- Ajouter des logs structurés pour chaque étape avec `trade_id`, `idempotency_key`, mint, wallet, mode et résultat.
- Ne pas remplacer un composant réseau sans vérifier sa compatibilité avec le code actuel.

### 5. Gestion des positions
- Vérifier l’ouverture, la fermeture et la restauration des positions.
- Tester TP, SL, trailing stop, max loss, partial sell, auto-sell et buy-on-dev-sell.
- Éviter les ventes répétées et les conflits entre stratégies de sortie.
- Définir une priorité déterministe des règles de sortie et l’enregistrer dans les logs.

### 6. Performance et observabilité
- Éviter les appels bloquants dans le hot path.
- Utiliser des tâches asynchrones correctement annulables.
- Ajouter métriques : latence événement→signal, signal→build, build→broadcast, taux d’échec, retries, gaps de flux et positions ouvertes.
- Ajouter un mode diagnostic sans signature ni broadcast.

### 7. Tests et CI
- Ajouter des tests unitaires pour le risk engine, l’idempotence, les conversions de montants, les règles TP/SL et les erreurs réseau.
- Ajouter des tests d’intégration simulés pour les flux buy/sell.
- Tester les cas limites : NaN/inf, montant nul/négatif, solde insuffisant, perte journalière dépassée, événement dupliqué, timeout et redémarrage.
- Faire passer formatage, lint et pytest dans GitHub Actions.

### 8. Documentation et configuration
- Documenter les variables d’environnement dans `.env.example` sans secret réel.
- Décrire clairement PAPER/DRY-RUN/LIVE et les conditions d’activation.
- Mettre à jour le README avec installation, démarrage, tests, sécurité, observabilité et rollback.
- Ajouter un rapport d’audit indiquant les risques restants et les points non vérifiés.

## Règles de livraison
1. Faire les changements par petits commits lisibles.
2. Ne pas supprimer une fonctionnalité existante sans justification et test.
3. Ne pas activer LIVE, ne pas signer de transaction réelle et ne pas utiliser de fonds réels.
4. Exécuter tous les tests disponibles et corriger les régressions.
5. Fournir à la fin : résumé des fichiers modifiés, commandes exécutées, résultats des tests, risques restants et procédure exacte d’activation manuelle du LIVE.
6. Si une hypothèse est nécessaire, la documenter au lieu de la cacher.
