# Revue d'intégration des PR 1 à 4

Date de vérification GitHub : 22 novembre 2026. Cette revue compare les branches publiées avant les modifications de cette mission.

## État des PR

| PR | État GitHub | Branche de tête | Base | Constat |
|---:|---|---|---|---|
| 1 | Ouverte, brouillon | `improvements/safety-observability` (`62329e9`) | `main` (`fcd8a49` au moment de création) | Garde-fous fail-closed, intents persistants, sorties et anti-rug |
| 2 | Ouverte, brouillon | `nextgen/performance-observability` (`998b61b`) | PR 1 | Télémétrie p50/p95/p99 et déduplication/reprise copy-trade |
| 3 | Ouverte | `codex/audit-securite-fiabilite` (`fe396d1`) | `main` (`79ab6c2`) | Première branche de sécurité |
| 4 | Ouverte | `codex/bot-reliability-phase-2` (`e234c73`) | PR 3 | Fiabilité, comptabilité et garde-fous supplémentaires |

Aucune de ces PR n'était fusionnée lors de la vérification. Les PR 3 et 4 sont empilées. Les branches 1/2 sont parallèles et ne sont pas les ancêtres de la branche 4.

## Apports à préserver

| Apport publié | Présence dans le head PR 4 avant cette mission | Traitement sur cette branche |
|---|---|---|
| `safety_guard.py` et `safety_runtime.py` de PR 1 : préflight, intents persistants, idempotence et exposition | Absents. PR 4 a `risk_controls.py` et un registre `pending_transactions`, mais pas la même machine d'états d'intents. | Pas de cherry-pick global : les modèles de persistance et plusieurs fichiers cœur divergent. Les signatures pendantes restent fail-closed. L'intégration complète de ces modules reste à traiter séparément. |
| `intent_reconciliation.py` de PR 2 : rapprochement opérateur des achats/ventes non comptabilisés | Absent. PR 4 ne classe que les signatures présentes dans une position; les autres restent manuelles. | Ajout d'un inspecteur RPC en lecture seule, qui retire uniquement les échecs finalisés avec frais vérifiés. Les succès non comptabilisés restent bloqués. Le module d'intents de PR 2 n'a pas été copié tel quel. |
| `execution_metrics.py` de PR 2 : mesures de latence p50/p95/p99 | Absent. RPC avait une fenêtre de latence interne, sans métriques de phases d'exécution. | Réimplémenté avec file d'écriture bornée non bloquante, liste blanche de labels, voies RPC primaire/repli et résumé p50/p95/p99; intégré aux étapes WebSocket, stratégie, Jupiter et RPC. Commande Telegram `/performance` lit hors de la boucle asyncio. |
| Tests de déduplication copy-trade de PR 2 | Le listener actuel a déjà un cache et des tests de traitement, mais l'historique PR ne suffit pas à démontrer l'équivalence fonctionnelle complète. | Conservés sans remplacement; leurs callbacks sont instrumentés pour mesurer la durée. Les tests restent mockés et ne valent pas un soak fournisseur. |
| Détection de migration, gestion des ventes et protections anti-rug de PR 1 | Plusieurs mécanismes apparentés existent dans la branche 4, avec un adaptateur GoPlus durci; les deux branches ne partagent pas leur implémentation complète. | Pas de remplacement des protections existantes. Les différences de schéma et le cycle de vente crash-safe restent à vérifier avant toute intégration des fichiers PR 1. |

## Pourquoi aucune fusion globale des PR 1/2

Une comparaison de branches montre des divergences importantes dans `jupiter_executor.py`, `live_trader.py`, `paper_trader.py`, `security.py`, `monitoring_list.py`, `main.py` et leurs tests. Un merge global ferait remplacer ou supprimer des validations et tests présents dans PR 3/4. Cette mission reste basée sur le head PR 4 et ne fusionne aucune PR.

## Vérification locale de départ

Avant modification, le worktree propre du head PR 4 réussissait **318 tests** avec `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`. Le checkout Bureau `main` n'a pas été utilisé pour les modifications : il contenait 49 chemins modifiés/non suivis et reste intact.
