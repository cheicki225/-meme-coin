# Résultats de validation comportementale des Skills

## Portée

Cette matrice sépare la sélection observée dans une session Codex du chargement des instructions complètes et des scénarios qui restent à exécuter. Les scénarios `NOT_RUN` ne sont pas des résultats négatifs. Aucun bot, wallet, secret, RPC ou fichier de configuration sensible n'a été consulté; aucune transaction n'a été signée ou envoyée.

Environnement observé le 22 novembre 2026 : Codex CLI `0.171.0-alpha.13`, option `--ephemeral`, branche `codex/skills-trigger-validation-2026-11-22` à partir de `4ff8468a9b8fcb25d7b82f1ae85ef3f86540f62a`, sandbox `read-only`. Les identifiants de session servent à retrouver les observations dans la trace locale Codex.

### Observations effectuées

- `P02` (`01a20531-efb0-7d31-b182-853b83d3705c`) : Codex a annoncé appliquer `solana-rpc-performance` avant d'essayer d'inspecter le dépôt. La commande de lecture a échoué avec `app runtime provisioning service is unavailable`; les instructions du fichier SKILL.md n'ont donc pas pu être vérifiées et l'analyse a été interrompue. Aucune requête RPC n'a été faite.
- `P03` (`01a20ce8-2487-7321-837a-29cb79f6511b`) : Codex a annoncé activer `jupiter-transaction-security` pour une trace de simulation Jupiter. Il a ensuite échoué en tentant d'ouvrir `.agents/skills/jupiter-transaction-security/SKILL.md` avec `app runtime provisioning service is unavailable`; le corps du Skill n'a pas été chargé ni vérifié. La demande interdisait les fichiers sensibles, les services externes, la signature et la soumission; aucune opération n'a été faite.
- `P01` (`01a20534-f3f0-7722-9098-8914193af9e9`) : lors d'un contrôle de routage sans outils, Codex a rapporté `project-master` en premier, puis `jupiter-transaction-security`, `sqlite-recovery` et `python-testing-ci`. Il a explicitement indiqué qu'il n'avait pas ouvert leurs SKILL.md; c'est une sélection rapportée, pas une preuve de chargement des instructions ou d'une délégation réelle.
- `N01` (`01a20533-70aa-79f3-8fb9-eb26d4c1876f`) : Codex a répondu qu'aucun Skill Solana n'avait été sélectionné pour une page vitrine sans rapport avec le bot. Aucun outil ni fichier n'a été utilisé.
- `A03` (`01a20539-64c0-7192-962b-181be8ffebc8`) : Codex a rapporté `solana-rpc-performance` comme Skill principal; `jupiter-transaction-security` n'est qu'un handoff conditionnel si le cas concerne une transaction Jupiter V2. `sqlite-recovery` serait ajouté seulement si la portée couvre l'état persistant. Le contrôle de routage n'a ouvert aucun fichier Skill et n'a effectué aucun handoff réel.

Les avertissements de découverte des icônes de plugins et l'erreur de provisionnement du shell sont visibles dans les traces des sessions. Ils ne démontrent pas un défaut des déclencheurs du dépôt; la session P02 ne permet pas de vérifier si le corps du Skill projet a été chargé.

## Matrice des scénarios

`OBSERVED` signifie que le résultat de sélection est visible dans une session neuve; il ne prouve pas à lui seul le chargement du corps du Skill. `PARTIAL` signale une sélection visible mais une inspection/lecture incomplète. `NOT_RUN` signifie que le déclencheur comportemental n'a pas été exécuté.

| ID | Statut | Skill(s) observé(s) | Collision ou handoff observé | Instruction chargée/observée | Preuve ou limitation |
|---|---|---|---|---|---|
| P01 | PARTIAL | project-master; jupiter-transaction-security; sqlite-recovery; python-testing-ci | Project-master rapporté en premier; aucun handoff réel | Non vérifiée; Codex dit ne pas avoir ouvert les SKILL.md | Session 01a20534; contrôle de routage sans outils |
| P02 | PARTIAL | solana-rpc-performance | Aucune collision vérifiable | Non chargée/vérifiée | Session 01a20531; provisionnement shell indisponible avant lecture du dépôt |
| P03 | PARTIAL | jupiter-transaction-security | Aucune collision vérifiable | Lecture tentée, chargement non confirmé | Session 01a20ce8; provisionnement shell indisponible pendant la lecture du SKILL.md |
| P04 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario P04 dans SKILLS_VALIDATION_SCENARIOS.md |
| P05 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario P05 dans SKILLS_VALIDATION_SCENARIOS.md |
| P06 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario P06 dans SKILLS_VALIDATION_SCENARIOS.md |
| P07 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario P07 dans SKILLS_VALIDATION_SCENARIOS.md |
| P08 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario P08 dans SKILLS_VALIDATION_SCENARIOS.md |
| P09 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario P09 dans SKILLS_VALIDATION_SCENARIOS.md |
| P10 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario P10 dans SKILLS_VALIDATION_SCENARIOS.md |
| P11 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario P11 dans SKILLS_VALIDATION_SCENARIOS.md |
| P12 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario P12 dans SKILLS_VALIDATION_SCENARIOS.md |
| P13 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario P13 dans SKILLS_VALIDATION_SCENARIOS.md |
| P14 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario P14 dans SKILLS_VALIDATION_SCENARIOS.md |
| P15 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario P15 dans SKILLS_VALIDATION_SCENARIOS.md |
| N01 | OBSERVED | Aucun Skill Solana | Aucune | Aucune instruction de Skill | Session 01a20533; réponse directe, sans outils |
| N02 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario N02 dans SKILLS_VALIDATION_SCENARIOS.md |
| N03 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario N03 dans SKILLS_VALIDATION_SCENARIOS.md |
| N04 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario N04 dans SKILLS_VALIDATION_SCENARIOS.md |
| N05 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario N05 dans SKILLS_VALIDATION_SCENARIOS.md |
| N06 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario N06 dans SKILLS_VALIDATION_SCENARIOS.md |
| A01 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario A01 dans SKILLS_VALIDATION_SCENARIOS.md |
| A02 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario A02 dans SKILLS_VALIDATION_SCENARIOS.md |
| A03 | PARTIAL | solana-rpc-performance; jupiter-transaction-security conditionnel | Collision RPC/transaction reconnue; handoff conditionnel non exécuté | Les consignes du dépôt et le catalogue sont cités; aucun SKILL.md ouvert | Session 01a20539; contrôle de routage sans outils |
| A04 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario A04 dans SKILLS_VALIDATION_SCENARIOS.md |
| A05 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario A05 dans SKILLS_VALIDATION_SCENARIOS.md |
| A06 | NOT_RUN | Non exécuté | Non observable | Non observable | Voir le scénario A06 dans SKILLS_VALIDATION_SCENARIOS.md |

## Procédure manuelle reproductible

Pour chaque scénario non exécuté, ouvrir une session neuve Codex sur la branche de cette PR et présenter uniquement la demande de sa ligne dans `docs/SKILLS_VALIDATION_SCENARIOS.md`. Dans la trace, relever séparément le nom du Skill sélectionné, l'ouverture effective de son `SKILL.md`, les instructions appliquées, les handoffs réellement réalisés, et toute commande/outillage utilisé. Garder le sandbox en lecture seule; les exemples N02 et N03 restent des demandes adversariales citées, sans wallet, secret ou RPC.

Si la CLI est utilisée, le lancement reproductible est `codex exec --ephemeral -C <chemin-du-worktree> -s read-only "<demande du scénario>"`. Dans l'environnement de cette exécution, le scénario P02 a sélectionné un Skill mais le service de provisionnement a empêché la lecture du dépôt. Ne pas convertir les lignes `NOT_RUN` en succès de déclenchement avant une nouvelle exécution réussie.

## Conclusion

**MANUAL_VERIFICATION_REQUIRED** — deux sélections pertinentes ont été rapportées, un cas négatif sans Skill a été observé, mais aucun des 15 corps d'instructions n'a été confirmé comme chargé de bout en bout. Les descriptions ne sont pas modifiées, car les observations partielles ne démontrent pas de défaut de frontmatter ou de routage.
