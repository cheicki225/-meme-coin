# Monitoring et alertes

## Fonctions présentes

- `rpc_client.get_metrics()` expose compteurs d'appels, erreurs, fallback et latences, sans retourner les URL ni clés configurées.
- `rpc_health_monitor.py` surveille les erreurs et latences agrégées; `notifier.py` transmet des alertes via Telegram avec repli texte.
- Les catégories d'alerte incluent RPC, collecteur, risque et erreurs de trading; `/halt` et `/resume` contrôlent le kill switch.
- Le collecteur limite les alertes répétées par cooldown.
- Les exceptions de fournisseurs et RPC sont journalisées par type plutôt qu'avec le texte brut, car certaines URL contiennent des clés.

## Accès et limites

Les commandes Telegram sont privées et vérifiées par `TELEGRAM_ALLOWED_USER_IDS`; sans allowlist, les commandes sont refusées. Les métriques sont en mémoire et réinitialisées au redémarrage; aucune télémétrie externe ni SLO n'est configuré par le dépôt. La livraison d'alertes doit encore être validée dans l'environnement cible. Ne jamais ajouter une clé, une URL d'accès, une clé privée ou un corps HTTP brut aux logs.
