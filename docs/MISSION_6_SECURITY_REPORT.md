# Mission 6 — rapport de sécurité indépendant

Date : 22 novembre 2026. Les constats indépendants initiaux proviennent d'une revue statique de la tête PR #5. Les corrections autorisées ont ensuite été appliquées dans la branche Mission 6 et vérifiées par tests isolés.

## Résumé

Deux problèmes ont été corrigés : le downgrade d'un état SQLite engagé vers une ancienne sauvegarde JSON après corruption sans backup SQLite valide, et l'acceptation de valeurs non finies ou invalides pour `CIRCUIT_BREAKER_USD`. Les swaps refusent aussi par défaut une trace de simulation CPI absente ou un programme CPI externe non autorisé.

La garde CPI filtre les identifiants observés dans une simulation; elle n'audite pas la sémantique ni le code de chaque programme DEX. Le débit de token exact reste vérifié après confirmation. L'arrêt d'urgence bloque aussi les ventes; il ne liquide pas les positions. Rien ne permet de déclarer LIVE sûr.

## Constats

| ID | Sévérité | Constat | Statut |
|---|---|---|---|
| SEC-01 | Critique avant correction | Une base SQLite engagée corrompue sans backup SQLite valide pouvait se replier sur un vieux JSON et perdre une signature en attente ou l'arrêt d'urgence. | Corrigé; test de régression et processus tué après commit. |
| SEC-02 | P2 avant correction | `CIRCUIT_BREAKER_USD=NaN`/Inf pouvait rendre les comparaisons du coupe-circuit fausses. | Corrigé par validation positive et finie au chargement; NaN, Inf, zéro, négatif et non numérique testés. |
| SEC-03 | Élevée / ouverte | Les programmes CPI/DEX n'étaient pas vérifiés par les contrôles top-level. | Réduit par simulation RPC et allowlist fail-closed; audit DEX sémantique et hash du programme toujours absents. |
| SEC-04 | Élevée / ouverte | Le débit exact de token est contrôlé après le swap confirmé. | Documenté; les écarts bloquent la comptabilisation et restent à rapprocher, mais l'effet du swap a déjà eu lieu. Les tokens à frais de transfert ne sont pas validés avant le swap. |
| SEC-05 | Opérationnelle | L'arrêt d'urgence bloque achats et ventes; aucune liquidation n'est initiée. | Limite documentée, inchangée. |
| SEC-06 | Préparation LIVE | Aucune transaction signée simulée sur un endpoint RPC réel; l'endpoint et la clé Helius opérationnelle ne sont pas vérifiés. | LIVE reste bloqué. |

## Parcours transaction Jupiter

1. Le routeur de premier niveau et les champs de quote/devis sont validés avant signature; les redirections HTTP sont désactivées pour Order et Execute.
2. Avant signature, le binaire produit par Jupiter est simulé avec `sigVerify=false`, `replaceRecentBlockhash=true` et `innerInstructions=true` via le RPC configuré.
3. Le contrôle refuse si l'appel RPC échoue, le résultat indique une erreur, la trace CPI est absente/incomplète, les index de compte sont hors plage, le programme Jupiter n'est pas l'instruction externe attendue, ou un programme externe n'est pas dans `JUPITER_CPI_PROGRAM_ALLOWLIST`.
4. L'allowlist est vide par défaut. Les programmes système/token courants sont traités comme programmes core; un identifiant seul ne prouve pas qu'une mise à jour de programme est inoffensive.
5. Les tests utilisent des réponses synthétiques. Ils ne consultent pas Solana et n'effectuent aucune transaction.

La documentation Solana indique que `simulateTransaction` n'est pas diffusée et peut renvoyer `innerInstructions`; les métadonnées de transactions confirmées documentent aussi instructions internes et frais ([simulateTransaction](https://solana.com/docs/rpc/http/simulatetransaction), [JSON Structures](https://solana.com/docs/rpc/json-structures)). Ces documents décrivent les champs RPC, pas la sécurité des programmes appelés.

L'allowlist ne décode pas complètement les données binaires de chaque instruction interne, ne vérifie pas que chaque CPI est économiquement attendu, ne valide pas toutes les adresses de compte selon la sémantique DEX, et ne pinne pas le hash du code déployé. Les programmes autorisés doivent rester non configurés tant qu'ils n'ont pas été revus. Aucun identifiant DEX de production n'est ajouté par ce travail.

## Transactions incertaines

L'incertitude après soumission ne déclenche aucune retransmission automatique. Les lectures `getSignatureStatuses`/`getTransaction` ne modifient pas l'état chaîne; signatures absentes/non finalisées restent pending. Une réussite finalisée sans position durable reste à rapprocher au lieu de fabriquer une position à partir de deltas incomplets. Une erreur de sauvegarde maintient l'arrêt et conserve le pending.

## Tests des corrections

- CPI : allowlist vide, programme inconnu, trace manquante, erreur de simulation et index invalides refusés; programme allowlisté accepté par fixture.
- Configuration : NaN, Infinity, zéro, négatif et entrée invalide refusés; valeur positive finie acceptée.
- SQLite : sauvegarde JSON périmée refusée après corruption d'un état déjà engagé; erreur d'écriture simulée conserve le dernier snapshot commité.
- Processus : l'enfant écrit une signature pending puis quitte via `os._exit`; le parent redémarre DataStore et le chemin recovery retrouve l'arrêt et le pending.
- Suite complète après modifications : 378 tests réussis; aucune transaction réelle ni endpoint externe.

## Conclusion

Les preuves justifient les tests et gardes de cette branche. Elles ne justifient pas « transactions Jupiter entièrement auditées », « sécurité DEX vérifiée » ou « LIVE prêt ». Voir [le rapport de récupération](MISSION_6_RECOVERY_REPORT.md).
