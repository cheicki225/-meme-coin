# Catalogue des Skills Codex

La référence officielle de Codex documente .agents/skills/ comme chemin des Skills de projet et ~/.agents/skills/ comme chemin utilisateur. Les versions observées ici sont Codex CLI 0.171.0-alpha.13 et application desktop 26.1117.4957.0. Une nouvelle session CLI ouverte sur cette branche a exécuté un sous-ensemble de routage; le chargement complet et le déclenchement des 15 Skills restent à valider. Les observations et limites sont consignées dans [la matrice comportementale](docs/SKILLS_BEHAVIORAL_RESULTS.md). Référence : [Skills dans Codex](https://developers.openai.com/api/reference/cli). Les Skills n'accordent pas d'accès ou d'autorisation opérationnelle.

## Les 15 Skills du dépôt

| Skill | Fichier | Déclencheur principal | Handoff principal |
|---|---|---|---|
| project-master | .agents/skills/project-master/SKILL.md | Demande couvrant plusieurs domaines, intégration ou plan de mission | Spécialiste du ou des modules concernés |
| solana-rpc-performance | .agents/skills/solana-rpc-performance/SKILL.md | RPC HTTP public, 429/5xx, retry, fallback, limites, WebSocket ou latence | production-readiness-auditor pour une décision d'exploitation |
| jupiter-transaction-security | .agents/skills/jupiter-transaction-security/SKILL.md | Quote V2, instruction, signature, CPI/DEX, frais, confirmation ou signature ambiguë | sqlite-recovery pour l'état durable; trading-risk-manager pour les arrêts |
| token-risk-antirug | .agents/skills/token-risk-antirug/SKILL.md | Autorités token, Token-2022, GoPlus, liquidité/LP, TVL ou heuristiques anti-rug | jupiter-transaction-security pour la transaction; trading-risk-manager pour admission |
| wallet-intelligence-collector | .agents/skills/wallet-intelligence-collector/SKILL.md | Historique, traçage, activité, collecte read-only, curseurs et candidats wallet | copy-trading lorsque l'événement observé devient un signal de copie |
| copy-trading | .agents/skills/copy-trading/SKILL.md | Wallets suivis, buy/sell observés, déduplication et callbacks de copie | wallet-intelligence-collector pour l'historique; trading-risk-manager pour admission |
| trading-risk-manager | .agents/skills/trading-risk-manager/SKILL.md | Admission, exposition, pertes, sizing, coupe-circuit, halt/resume | sqlite-recovery pour persistance; jupiter-transaction-security pour exécution |
| multi-wallet-execution | .agents/skills/multi-wallet-execution/SKILL.md | Wallets gérés, MultiBuy/MultiSell, propriété et comptabilité par wallet | pnl-accounting et jupiter-transaction-security |
| pnl-accounting | .agents/skills/pnl-accounting/SKILL.md | Flux SOL/token, frais, coût de base, PnL réalisé ou rapprochement | backtesting-validation pour les résultats historiques |
| backtesting-validation | .agents/skills/backtesting-validation/SKILL.md | Dataset OHLC, replay, frais/slippage, gaps, drawdown ou lookahead | pnl-accounting pour le grand livre de trades |
| telegram-monitoring | .agents/skills/telegram-monitoring/SKILL.md | Commandes Telegram, allowlist, alertes, métriques ou santé RPC | trading-risk-manager, sqlite-recovery ou copy-trading selon la sémantique |
| python-testing-ci | .agents/skills/python-testing-ci/SKILL.md | pytest, fixtures, couverture, Ruff, pip-audit, Gitleaks ou GitHub Actions | Skill métier concerné pour les invariants testés |
| sqlite-recovery | .agents/skills/sqlite-recovery/SKILL.md | SQLite, ancien JSON, sauvegardes, migrations, crash ou état pending | trading-risk-manager et jupiter-transaction-security |
| f-project-benchmark | .agents/skills/f-project-benchmark/SKILL.md | Comparaison du bot avec F Project, fonctionnelle ou mesurée | solana-rpc-performance, backtesting-validation ou production-readiness-auditor |
| production-readiness-auditor | .agents/skills/production-readiness-auditor/SKILL.md | Question de préparation au déploiement, à PAPER ou à LIVE | Spécialiste du contrôle manquant; project-master pour plusieurs domaines |

## Règles de routage

- Pour une demande ciblée, activer le Skill du domaine principal. Ajouter un handoff uniquement lorsque la tâche atteint réellement un second domaine.
- Les frontières se fondent sur l'objet du changement : observations et historiques de wallets relèvent du collecteur; événements buy/sell qui pilotent la copie relèvent de copy-trading; les commandes et notifications relèvent de Telegram; la décision d'admission et les limites relèvent de risk manager.
- Les replays OHLC et résultats de stratégie relèvent de backtesting; le coût de base, les frais et les trades effectivement comptabilisés relèvent de PnL.
- Un rapport de benchmark ne transforme pas des mesures synthétiques en mesures réseau. Une demande « prêt pour LIVE » relève de production-readiness-auditor et doit conserver le blocage LIVE tant que les preuves de LIVE_READINESS.md manquent.

## Aide externe conditionnelle

web-research-problem-solver n'est pas l'un des 15 Skills livrés et aucun SKILL.md de ce nom n'existe dans le dépôt de référence. Ne pas le recopier ou l'invoquer par défaut. Si un blocage technique précis reste indécidable à partir du code et des documents actuels et exige des sources à jour, vérifier d'abord si cette compétence de recherche sourcée est réellement disponible dans la session. L'utiliser uniquement pour ce blocage; si elle manque, le signaler et ne pas simuler la recherche. Aucune recherche de ce type n'a été nécessaire à la création de cette bibliothèque.
