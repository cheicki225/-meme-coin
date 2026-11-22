# Revue de sécurité Jupiter

Date de revue : 22 novembre 2026. Revue statique et tests unitaires isolés sur la branche `codex/mission-performance-2026-11-22`, issue du head de la PR #4 (`e234c73172f7af422bdd1de93008513449e5cb70`). Aucun secret n'a été lu et aucune transaction n'a été signée ou diffusée pendant cette mission.

## Résumé

Un défaut HTTP important a été corrigé : `aiohttp` suivait les redirections par défaut alors que les requêtes Jupiter transportent `x-api-key` et que `/execute` transporte une transaction signée. Les redirections sont maintenant refusées sur les deux appels. La revue n'établit pas la sûreté de toutes les instructions CPI/DEX ni la préparation LIVE.

## JUP-001 — Redirection HTTP pouvant exposer des données sensibles

- **Sévérité : élevée avant correction. Statut : corrigé et testé.**
- **Emplacements :** `jupiter_executor.py:440-472` pour la requête d'ordre et `jupiter_executor.py:521-539` pour la soumission.
- **Constat :** les appels `GET /swap/v2/order` et `POST /swap/v2/execute` transmettent la clé `x-api-key`; la seconde requête transmet aussi `signedTransaction`. L'option par défaut d'aiohttp est `allow_redirects=True`.
- **Impact :** une réponse HTTP 3xx pouvait provoquer une seconde requête vers une destination redirigée avec les paramètres ou le corps sensibles.
- **Correction :** `allow_redirects=False` sur les deux appels. Toute réponse autre que HTTP 200 échoue fermé; aucune deuxième destination n'est appelée.
- **Tests :** `tests/test_jupiter_redirect_security.py` vérifie les options GET/POST et le refus des réponses 302. Les tests remplacent la session HTTP; aucun appel externe ni swap n'est réalisé.

## Contrôles présents et vérifiés statiquement

- Les URL d'ordre et d'exécution sont constantes dans `config.py`; les appels attendent la clé API et n'acceptent que les routes Metis.
- Le devis est lié aux mints, montants, minimum reçu, slippage et impact configurés avant signature.
- Pour une entrée par token non natif, le débit de token confirmé doit correspondre exactement au montant exact-in demandé. Un écart laisse la signature en attente et bloque l'exécution; les débits SOL sont comptabilisés à partir du delta natif, frais et loyer inclus.
- La transaction construite est inspectée avant signature : payeur/signataire, lookup tables, programmes top-level, ATA et transferts pris en charge.
- La signature locale est persistée avant l'appel à Jupiter. Les issues ambiguës restent en attente et aucun renvoi automatique n'est ajouté.
- Les échanges HTTP et RPC n'enregistrent pas le corps signé, la clé API, la signature ou les réponses fournisseur dans les métriques.
- La documentation officielle Jupiter confirme les chemins Swap API V2 `/swap/v2/order` et `/swap/v2/execute`, le header `x-api-key`, et les champs envoyés à l'exécution (`signedTransaction`, `requestId`, `lastValidBlockHeight`). Voir [Order](https://developers.jup.ag/docs/api-reference/swap/order) et [Execute](https://developers.jup.ag/docs/api-reference/swap/execute).

## Limites restantes

1. La validation ne désassemble pas ni n'audite programme par programme les CPI déclenchés par le routeur et les DEX. L'autorisation top-level et le contrôle du route plan ne prouvent pas la sûreté sémantique de tous les programmes appelés.
2. Aucun test de transaction signée n'a été simulé sur RPC, devnet ou mainnet. Aucun swap n'a été exécuté.
3. Une réussite finalisée sans position durable reste en attente. La commande `/reconcile` peut reconnaître l'état finalisé, mais ne crée pas une position approximative à partir d'une cotation.
4. Les frais des transactions finalisées en échec sont stockés en lamports, mais ne sont pas convertis ni déduits automatiquement du PnL USD. `/stats` les signale séparément.
5. Les signatures absentes, non finalisées ou dont les métadonnées sont incomplètes restent bloquées. La commande ne réactive jamais automatiquement le bot; `/resume` reste une action distincte après examen humain.

## Préparation d'exécution

`TRADING_MODE=PAPER` reste le défaut. `LIVE_EXECUTION_ENABLED` reste une seconde barrière explicite. Les nouveaux tests sont entièrement simulés; la préparation LIVE demeure non vérifiée.

## Addendum Mission 6 — simulation des CPI (22 novembre 2026)

Une garde complémentaire a été ajoutée dans `jupiter_executor.py` : avant signature, le bot demande au RPC une simulation sans diffusion avec `innerInstructions=true`, puis refuse si le résultat échoue, si la trace interne est absente/incomplète ou si un programme CPI externe n'est pas dans `JUPITER_CPI_PROGRAM_ALLOWLIST`. Cette liste est vide par défaut. Des tests synthétiques sont dans `tests/test_jupiter_cpi_audit.py`; aucun RPC réel ni swap n'a été appelé.

Ce changement réduit le risque identifié, mais ne transforme pas le filtre en audit complet des instructions DEX. Le code de l'instruction interne, les effets économiques, les comptes selon la sémantique DEX et la provenance/version de programme ne sont pas audités par cette garde. `simulateTransaction` reflète l'état observé par un noeud et n'envoie pas de transaction; une transaction ultérieure peut rencontrer un état ou un route différent. Les tokens à frais de transfert ne sont pas validés avant swap; le contrôle de débit exact demeure après confirmation. LIVE reste non prêt. Rapport complet : [Mission 6, revue de sécurité](MISSION_6_SECURITY_REPORT.md).
