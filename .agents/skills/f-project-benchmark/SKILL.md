---
name: f-project-benchmark
description: "Design, reproduce, or review a fair comparison between this bot and F Project using traceable measures and explicit limits."
---

# Benchmark F Project

## Déclencheurs

Utilise ce skill lorsque la demande compare la vitesse, la fiabilité, le risque, le PnL, la couverture fonctionnelle ou la préparation opérationnelle du bot à F Project. Distingue toujours une comparaison fonctionnelle d’une comparaison mesurée.

## Audit avant toute modification

- Lire la branche, le diff et les documents actuels avant de remplacer un rapport.
- Examiner [COMPETITIVE_BENCHMARK.md](../../../COMPETITIVE_BENCHMARK.md), [docs/MISSION_6_BENCHMARK.md](../../../docs/MISSION_6_BENCHMARK.md), [scripts/mission6_paper_benchmark.py](../../../scripts/mission6_paper_benchmark.py), [execution_metrics.py](../../../execution_metrics.py), rpc_client.py et les tests de métriques/retry.
- Vérifier le commit source, l’horodatage, le fichier de résultats et la méthode correspondant à chaque mesure. Ne réutilise pas un nombre d’un autre commit comme résultat actuel.
- Séparer les affirmations commerciales de F Project des mesures reproductibles, et documenter la date de consultation des sources officielles.

## Workflow

1. Énoncer la question, le périmètre et la comparaison visée: fonction disponible, comportement testé, latence, fiabilité, coût, précision ou récupération. Ne fusionne pas ces catégories.
2. Pour une mesure locale sans réseau, utiliser le script PAPER existant avec un résultat de sortie dans un répertoire temporaire, par exemple scripts/mission6_paper_benchmark.py --cycles 10000 --output <répertoire-temporaire>/paper-soak.json. Le script exige au moins 100 cycles et doit rester sur ses serveurs loopback.
3. Rapporter les conditions avec la mesure: commit, Python, système, nombre d’observations, temps mural, p50/p95/p99, échecs et dénominateur, retries/fallback, coût simulé et provenance des données.
4. Si une affirmation actuelle sur F Project est nécessaire, vérifier la source officielle et sa date. Attribuer explicitement les chiffres au fournisseur. Ne déduis pas un taux de réussite, une latence ou une supériorité à partir d’une vidéo ou d’une promesse marketing.
5. Pour une comparaison expérimentale, exiger une méthode commune: mêmes événements et fenêtre, définition de départ/fin, horloges validées par slots, échecs inclus, coûts en lamports et répétitions. Si les traces ou protocoles équivalents n’existent pas, conclure NON VÉRIFIABLE.
6. Conserver la sortie brute synthétique avec son commit et son empreinte si le rapport doit être reproductible; expliquer les scénarios injectés et les limites avant d’en tirer une conclusion.

## Sécurité

Le script de benchmark décrit dans le dépôt est PAPER seulement, vide les noms de clés connus dans son processus, utilise une base SQLite temporaire et deux serveurs HTTP sur 127.0.0.1. Vérifie ces garde-fous avant de le relancer après une modification.

Ne contacte jamais un RPC configuré, un wallet, Jupiter, un DEX, F Project ou Telegram pour exécuter un trade. N’utilise aucune clé ni secret. N’active pas LIVE. Un benchmark externe en lecture seule nécessiterait un périmètre et une autorisation distincts; il ne doit pas être ajouté implicitement au benchmark local.

Le catalogue du dépôt peut référencer web-research-problem-solver comme support conditionnel. Ne l’utilise que si un blocage technique précis exige une recherche sourcée pour continuer; une comparaison ordinaire n’est pas un tel blocage. S’il n’est pas disponible dans le contexte actif, indique-le et poursuis seulement avec les preuves déjà présentes.

## Tests

Après un changement du script, tester les invariants PAPER, l’usage du stockage temporaire, les faux serveurs loopback et l’absence d’hôte externe ou de soumission. Exécuter le script dans un répertoire temporaire; ne pas remplacer les résultats historiques suivis par Git sans consigne.

Pour les changements des statistiques, compléter par tests/test_execution_metrics.py, tests/test_rpc_metrics.py ou tests/test_rpc_retry_policy.py selon le chemin modifié. Aucun test dédié du script de benchmark n’est présent dans le dépôt observé; un lancement contrôlé du script ne valide que son scénario synthétique.

## Preuves attendues

Dans le rapport, inclure le commit et la commande exacte, le JSON produit, le type de données, la durée réelle, l’échantillon et ses percentiles. Nommer chaque élément comme SYNTHÉTIQUE, PUBLICATION FOURNISSEUR, RPC LECTURE SEULE ou MESURE OPÉRATIONNELLE. Ne pas dire que 24 heures virtuelles équivalent à un soak réel de 24 heures.

## Limites

Le run Mission 6 documenté exécute 10 000 cycles accélérés en local, avec HTTP loopback, événements synthétiques et frais fictifs; les 24 heures annoncées sont virtuelles. Ces mesures ne couvrent ni Solana, ni un WebSocket fournisseur, ni Jupiter, ni un DEX, ni l’inclusion de blocs, ni un trade complet.

Les sources F Project mentionnées dans les rapports n’offrent pas les traces brutes et définitions nécessaires à une comparaison de latence ou de taux d’échec reproductible. Aucune supériorité de vitesse, fiabilité, sécurité ou rentabilité n’est démontrée.

## Passages de relais

- RPC et instrumentation d’exécution: solana-rpc-performance.
- Métriques et suite de tests: python-testing-ci.
- Reprise et états SQLite: sqlite-recovery.
- Comptabilité des frais et PnL: pnl-accounting.
- Synthèse readiness multi-domaines: production-readiness-auditor.