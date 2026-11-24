---
name: telegram-monitoring
description: "Review or change this bot’s Telegram commands, notifications, RPC health alerts, or local execution telemetry."
---

# Telegram et monitoring

## Déclencheurs

Utilise ce skill lorsque la demande porte sur les commandes ou callbacks Telegram, l’allowlist des utilisateurs, l’envoi d’alertes, les métriques locales ou le moniteur de santé RPC.

Il couvre notamment /halt, /resume, /performance, /reconcile, /activity, les messages de notification et le rapport de latence. Pour la logique d’exécution copiée, passe le relais à copy-trading; pour les contrôles de risque, à trading-risk-manager; pour les transactions pendantes et SQLite, à sqlite-recovery.

## Audit avant toute modification

- Lire l’état Git, la branche et les éventuelles instructions du dépôt. Vérifier les changements locaux avant de modifier un fichier.
- Lire les chemins concernés dans [telegram_bot.py](../../../telegram_bot.py), [notifier.py](../../../notifier.py), [execution_metrics.py](../../../execution_metrics.py), [rpc_health_monitor.py](../../../rpc_health_monitor.py) et [config.py](../../../config.py).
- Vérifier les tests pertinents dans tests/test_telegram_ops_commands.py, tests/test_telegram_risk_status.py, tests/test_notifier_secret_redaction.py, tests/test_execution_metrics.py et tests/test_rpc_health_monitor.py.
- Vérifier les noms et valeurs fictives de .env.example seulement; ne jamais ouvrir ni afficher .env, ni lire les variables runtime contenant des tokens.

## Workflow

1. Identifier si la tâche touche au contrôle d’accès, à une commande opérationnelle, au transport des alertes ou aux métriques. Garder ces responsabilités distinctes.
2. Vérifier séparément chaque type d'update et chaque handler. Le build enregistre authorize_update via un MessageHandler et les boutons via un CallbackQueryHandler distinct; on_button ne contient pas de vérification explicite. Ne pas prétendre que l'allowlist protège les callbacks tant que le routage réel et des tests directs d'utilisateur autorisé/refusé et de chat privé ne le démontrent pas. Traiter cela comme une lacune de sécurité à signaler.
3. Pour /performance, préserver le rapport local borné et filtré. Les événements n’acceptent que des étapes et fournisseurs autorisés; le writer est asynchrone et best-effort, donc un événement perdu doit rester visible comme tel.
4. Pour les alertes, traiter le booléen de Notifier.notify comme un résultat de livraison. Ne pas supposer que notify_photo a le même contrat de retour.
5. Garder /activity en lecture seule et limité à un wallet déjà surveillé. Pour /reconcile, « lecture seule » décrit la chaîne: la commande active l'arrêt d'urgence lorsqu'il y a des signatures pendantes et peut modifier durablement l'état local en résolvant des signatures déjà rattachées ou des échecs finalisés. Elle ne signe ni ne soumet et ne lève pas l'arrêt; les états inconnus/succès non comptabilisés restent bloqués.
6. Avant d’ajouter une information au texte Telegram ou aux journaux, vérifier qu’elle ne révèle ni token, ni URL RPC, ni adresse privée, ni donnée opérationnelle inutile.

## Sécurité

PAPER reste le mode par défaut. N’envoie aucune commande à un bot Telegram réel, ne contacte pas un déploiement, ne signe ni ne diffuse de transaction et n’active pas LIVE. Utilise des faux objets et des transports simulés.

Ne logue jamais le texte d’une notification ni le contenu d’une exception Telegram: l’URL d’erreur peut contenir un token. Auditer les chemins qui violent cette règle: Notifier.notify_photo journalise actuellement la légende quand Telegram n'est pas connecté; ne pas conclure que toutes les notifications sont expurgées. N’affiche pas de clé, de secret, de signature complète ou d’adresse personnelle dans une réponse de monitoring. Ne contourne jamais authorize_update; vérifier sa couverture des callbacks avant de la revendiquer. Une commande d’arrêt d’urgence bloque de nouvelles opérations; elle ne liquide pas les positions.

## Tests

Pour une modification de commande ou d’autorisation, utiliser les tests isolés de telegram_bot et ajouter des cas pour l’utilisateur refusé, le chat non privé, l’état mal formé et les échecs de persistance concernés. Pour la télémétrie et les notifications, utiliser les fixtures locales de metrics et les faux bots; ne pas utiliser le réseau Telegram.

Les tests utiles sont:
- tests/test_telegram_ops_commands.py
- tests/test_telegram_risk_status.py
- tests/test_notifier_secret_redaction.py
- tests/test_execution_metrics.py
- tests/test_rpc_health_monitor.py

## Preuves attendues

Rapporter les fichiers inspectés et modifiés, les commandes exécutées, le résultat exact des tests et les chemins de fixtures. Distinguer clairement les tests simulés des messages effectivement envoyés: dans ce workflow, aucun bot déployé n’est contacté. Pour les métriques, mentionner leur fenêtre, leur nombre d’événements et les pertes éventuelles; ne pas les présenter comme un SLA de production.

## Limites

Les métriques locales sont best-effort et bornées; leurs percentiles ne prouvent ni la disponibilité de Telegram ni la latence d’un RPC déployé. Les tests existants simulent les adaptateurs. La commande /activity expose des événements observés, pas un ordre ni un PnL réalisé. /reconcile ne retransmet aucune transaction.

## Passages de relais

- RPC, erreurs HTTP et mesure de latence: solana-rpc-performance.
- Signatures pendantes, stockage et restauration: sqlite-recovery.
- Modifications d’un coupe-circuit ou d’une limite financière: trading-risk-manager.
- Tests et pipeline CI: python-testing-ci.
- Audit transversal avant toute affirmation de préparation: production-readiness-auditor.