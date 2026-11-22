# Limites de l'utilisation d'un RPC public

État du code vérifié le 15 novembre 2026. Les valeurs de l'environnement déployé restent inconnues.

## Ce que sélectionne le code

- `SOLANA_RPC_URL` remplace le point d'accès HTTP principal.
- Sinon, `HELIUS_API_KEY` construit une URL HTTP Helius.
- Sans ces deux valeurs, `config.py` utilise le RPC public mainnet Solana.
- `ALCHEMY_API_KEY` ajoute un secours HTTP facultatif, essayé après l'échec du primaire.
- Les WebSockets et l'API de transaction enrichie sont configurés uniquement à partir de `HELIUS_API_KEY` dans cette version. Aucun WebSocket public ou autre fournisseur n'est sélectionné automatiquement.

Le nom historique `HELIUS_RPC_URL` est donc trompeur lorsque `SOLANA_RPC_URL` ou le défaut public est utilisé. La valeur d'URL n'est jamais à afficher ni à journaliser : certains fournisseurs mettent une clé dans le chemin ou la query string.

## Capacités en mode public

Les méthodes JSON-RPC standards (ex. `getSignaturesForAddress`, `getTransaction`, `getAccountInfo`) peuvent fonctionner via HTTP public. Le collecteur en lecture seule et l'historique standard ne nécessitent plus une clé Helius. Sans Helius, la détection WebSocket de nouveaux tokens, le copy trade temps réel, le flux prix WebSocket et les transactions enrichies restent indisponibles.

Le bot peut alors démarrer en PAPER et affiche un avertissement de capacités dégradées. Le démarrage LIVE est refusé sans la configuration Helius utilisée par les WebSockets et le parsing enrichi. Une clé configurée n'est pas assimilée à une clé valide ou à un quota vérifié.

## Limites et comportement de reprise

- Solana indique que ses endpoints publics sont partagés, soumis à des quotas changeants et non destinés aux applications de production; ils peuvent répondre `429` ou `403`. Voir [Clusters et endpoints publics](https://solana.com/docs/references/clusters) et [Aperçu RPC](https://solana.com/docs/rpc).
- Le client local applique une limite globale conservatrice de 5 appels/s. Cette valeur ne connaît pas le quota réel du fournisseur et n'est pas réglée par endpoint.
- Les lectures RPC retentent les erreurs réseau, HTTP 429/5xx et certaines erreurs JSON-RPC transitoires, avec `Retry-After` et backoff avec jitter. Les erreurs permanentes ne sont pas répétées.
- Le secours Alchemy est séquentiel et optionnel. La fraîcheur des slots/réponses n'est pas contrôlée de façon suffisante pour autoriser du LIVE.
- La soumission d'une transaction avec résultat ambigu n'est jamais relancée automatiquement; la signature reste persistée et l'arrêt d'urgence empêche les swaps suivants jusqu'à la réconciliation.

## Vérification exigée au déploiement

Confirmer dans l'environnement cible les méthodes, quotas, politique d'historique, WebSocket, fraîcheur, latence et secours; exécuter des tests de charge PAPER et vérifier les alertes. Solana recommande un endpoint dédié et un secours testé pour les applications de production : [guide de choix d'un fournisseur RPC](https://solana.com/docs/rpc/providers).
