# Limites de l'utilisation d'un RPC public

État du code vérifié le 22 novembre 2026. Les valeurs de l'environnement déployé restent inconnues.

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
- Le client applique par défaut un espacement de 3 appels/s par hôte non-Enhanced, réglable avec `SOLANA_RPC_REQUESTS_PER_SECOND`; la même valeur vaut pour chaque hôte RPC standard, elle doit donc rester sous le plus petit quota configuré. L'API Helius Enhanced (`api.helius.xyz`) a un réglage séparé `HELIUS_ENHANCED_REQUESTS_PER_SECOND`, par défaut 1 appel/s, soit une marge sous son [quota Free publié de 2 appels/s](https://www.helius.dev/docs/billing/rate-limits). Les valeurs doivent être adaptées au forfait réellement utilisé et ne prouvent pas le quota du déploiement. Un HTTP 429 partage un cooldown en mémoire avec les autres appels au même hôte.
- Seules les méthodes de lecture explicitement allowlistées sont regroupées, retentées et admissibles au secours. Elles sont coalescées uniquement pendant leur requête en cours, si l'hôte et le délai d'attente sont les mêmes; aucun résultat n'est conservé après réponse. Les méthodes inconnues ou mutatrices ont un seul essai et aucun secours.
- Les lectures RPC allowlistées retentent les erreurs réseau, HTTP 429/5xx et certaines erreurs JSON-RPC transitoires. Les réponses HTTP 429 et erreurs JSON-RPC signalant explicitement une limite appliquent `Retry-After` (secondes/date HTTP) ou un backoff avec jitter et un cooldown partagé. Après un cooldown HTTP `Retry-After` supérieur à 30 secondes, le même hôte n'est pas retenté dans l'appel courant; une lecture peut essayer le secours configuré.
- Les méthodes de soumission (`sendTransaction`, `sendRawTransaction`, `sendBundle`) ont un seul essai côté client et n'utilisent pas le secours Alchemy. Un résultat ambigu doit rester pending jusqu'à réconciliation; aucune retransmission par le client n'est ajoutée par la politique RPC.
- Le WebSocket demeure Helius seulement. Les reconnexions après exception ou fermeture normale suivent un délai exponentiel avec jitter, borné à 30 secondes; `stop()` ferme la socket et interrompt le délai. Les notifications ne forment pas un journal durable et les événements manqués doivent être réconciliés par HTTP après reconnexion.
- Les métriques agrégées filtrées incluent les attentes de quota, lectures regroupées, délai de backoff avant reconnexion, envoi de souscriptions, parsing et callback de détection. Elles ne mesurent pas encore le handshake de reconnexion et ne corrèlent pas une détection particulière avec une décision, une soumission et sa confirmation.
- Si le parsing Enhanced renvoie HTTP 429, le cooldown est appliqué mais la signature consommée du flux WebSocket n'est pas encore mise en file de reprise. Cet événement peut être perdu; une file bornée et un rattrapage HTTP restent à concevoir.
- Le secours Alchemy est séquentiel et optionnel. La fraîcheur des slots/réponses n'est pas contrôlée de façon suffisante pour autoriser du LIVE.
- Le nœud RPC peut traiter/retransmettre une transaction signée selon les options présentes dans la requête; cette mission ne valide pas le comportement interne du fournisseur. Aucun appel de soumission n'a été effectué.

## Vérification exigée au déploiement

Confirmer dans l'environnement cible les méthodes, quotas, politique d'historique, WebSocket, fraîcheur, latence et secours; exécuter des tests de charge PAPER et vérifier les alertes. Les mesures de la machine Codex sont consignées séparément dans [le rapport Mission 8](docs/MISSION_8_PERFORMANCE.md) et ne qualifient pas le déploiement. Solana recommande un endpoint dédié et un secours testé pour les applications de production : [guide de choix d'un fournisseur RPC](https://solana.com/docs/rpc/providers).
