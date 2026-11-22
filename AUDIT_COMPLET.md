# Audit et avancement du bot Solana

## Revue complète de la PR n°3

La revue a couvert les 16 fichiers modifiés de la PR n°3 :

`.env.example`, `.github/workflows/ci.yml`, `ARCHITECTURE.md`, `AUDIT_COMPLET.md`, `CHANGELOG.md`, `CONFIGURATION.md`, `INSTALLATION.md`, `OPERATIONS.md`, `README.md`, `SECURITY.md`, `TRADING_GUIDE.md`, `config.py`, `jupiter_executor.py`, `telegram_bot.py`, `test_mobula_deployer.py`, `tests/test_jupiter_safety.py`.

Les principaux points relevés étaient l'utilisation des anciens endpoints Jupiter, la signature d'une transaction externe sans contrôle transactionnel complet, la dépendance à un état JSON, l'absence de reprise sûre après transaction ambiguë et le caractère approximatif du backtest historique. La PR n°3 est restée intacte; les corrections suivantes sont préparées sur une branche de suivi empilée dessus.

## Résultats de la phase de fiabilité

- **Jupiter et transactions :** migration vers Swap API V2 `/order` + `/execute`, clé API envoyée aux endpoints fixes, seul routeur Metis permis, version v0 et un signataire. Contrôles avant signature pour mints, montants, seuil de sortie exactement appliqué, slippage (plafond dur 10 %), impact (plafond dur 5 %), payeur, programmes top-level, tables d'adresses résolues, ATA du wallet, blockhash, frais et loyer estimé. LIVE est verrouillé par défaut derrière `LIVE_EXECUTION_ENABLED=false`. La signature est persistée avant l'exécution. Confirmation `finalized` exigée avant comptabilité; les résultats s'appuient sur les soldes réellement changés.
- **Jito :** les bundles avec transfert de tip dans une seconde transaction sont bloqués, car le compte de tip et les frais agrégés ne sont pas validés par la politique transactionnelle.
- **Wallet collector :** collecteur optionnel intégré au démarrage, strictement en lecture seule, par lots limités et avec curseur SQLite incrémental. Les événements identiques sont dédupliqués et les fetch RPC non exploitables peuvent être retentés; ces événements restent séparés des signaux qui déclenchent des transactions.
- **PnL et frais :** achats et ventes LIVE utilisent les deltas confirmés du wallet et la commission réseau; le cashflow SOL capture aussi les frais et le loyer réellement payés. PAPER inclut les frais estimés dans les coûts/cashflows. Les positions/totaux hérités sans historique vérifiable sont marqués incomplets sans être recalculés. La conversion USD dépend d'un cours SOL lu au moment du trade.
- **Persistance :** document SQLite en WAL et synchronisation FULL, import JSON initial sans écraser ce dernier, instantanés cohérents, récupération depuis la sauvegarde valide la plus récente et protection contre la rétrogradation du schéma.
- **Transactions ambiguës :** signature journalisée avant soumission; aucun retry automatique; une signature non résolue conserve l'arrêt d'urgence après redémarrage. MultiBuy LIVE reste refusé tant que chaque wallet n'est pas comptabilisé séparément.
- **RPC et alertes :** métriques de latence/erreur, surveillance par delta de compteurs avec gestion des remises à zéro, alertes réessayées si Telegram échoue et statut risque/pending accessible dans Telegram.
- **Backtest :** moteur de replay OHLC déterministe avec frais, slippage, règle stop-first et données versionnées/hashées. Les signaux peuvent être associés à un timestamp exact de bougie; les gaps traversés sont indiqués par trade. Le hash ne prouve pas la provenance, aucune ingestion fiable ni intégration complète à la stratégie n'est fournie.
- **GoPlus :** parsing du schéma Solana, champs EVM de honeypot/taxes laissés inconnus, rejet des réponses incomplètes/parts LP incohérentes et seuil par défaut de 10 000 USD sur le maximum de TVL rapporté quand le filtre est actif.
- **Risque :** expositions réservées sous verrou contre les achats concurrents, plafond de positions et d'exposition, arrêt persistant après dépassement de pertes, commandes Telegram `/halt` et `/resume`; transferts manuels bloqués en PAPER et plafonnés en LIVE. En LIVE, l'arrêt bloque les swaps d'achat et de vente et ne liquide pas les positions.

## Progression et limites au 22 novembre 2026

La revue des 16 fichiers de la PR #3 est consignée ci-dessus. Depuis cette revue, la branche de suivi ajoute la validation transactionnelle Jupiter, SQLite/récupération, collecte wallet, contrôles de risque et tests. La correction du 15 novembre durcit les nombres RPC/Jupiter, retries, journaux, mode RPC public dégradé et replay chronologique.

Le score actualisé et son barème sont maintenus dans [PROGRESS.md](PROGRESS.md): **72,5 % d'avancement de développement pondéré** (10 objectifs à poids égal; VERIFIED=100, TESTED=75, IMPLEMENTED=50, IN PROGRESS=25, NOT STARTED/BLOCKED=0). Il ne signifie ni disponibilité de production, ni résultat financier, ni readiness LIVE.

## États distincts

- **Développement :** 72,5 % selon le barème publié.
- **Tests locaux :** `compileall` réussi et 348 tests passés sur `codex/mission-performance-2026-11-22`; le head PR #4 de départ en passait 318. Voir [TESTING.md](TESTING.md). Aucun ordre réel n'a été diffusé.
- **Sécurité :** IN PROGRESS. CI #202 verte sur `0e4f100`, incluant lint, audit des dépendances et scan de secrets; l'audit indépendant CPI/DEX reste à faire.
- **PAPER :** mode par défaut. Les frais simulés d'entrée, DCA, sorties partielles/totales et Sell Initials sont désormais intégrés aux coûts/cashflows et testés; soak, restauration déployée et endpoint RPC cible restent à vérifier.
- **LIVE : BLOCKED / NOT READY.** Aucune transaction réelle exécutée. Aucun test mainnet, portefeuille financé, soak ni audit indépendant complet.

## Travaux encore ouverts

1. Exécuter un soak PAPER dans l'environnement cible; vérifier alertes Telegram, reprise après arrêt et restauration de sauvegarde.
2. Ajouter une réconciliation guidée de signatures pendantes à partir du RPC; garder l'arrêt tant que l'issue financière n'est pas certaine.
3. Relier le replay OHLC aux entrées historiques réellement produites par la stratégie et à des données dont la provenance est vérifiée; l'archive versionnée/hashée existe, mais son hash n'authentifie pas la source, les coûts de marché restent incomplets et le lookahead n'est pas validé de bout en bout.
4. Faire une revue CPI/DEX indépendante de la politique de transaction et vérifier le contrat de l'API Jupiter courant.
5. Mesurer les limites, fraîcheur et charge du fournisseur RPC réellement configuré; le déploiement n'a pas été inspecté.
6. Calibrer les frais simulés PAPER sur des observations documentées; le modèle actuel est déterministe en mode Simple et aléatoire en Hardcore, et ne remplace pas des frais de marché vérifiés.

Voir aussi [PROJECT_ROADMAP.md](PROJECT_ROADMAP.md), [RPC_PUBLIC_LIMITATIONS.md](RPC_PUBLIC_LIMITATIONS.md), [BACKTESTING.md](BACKTESTING.md) et [LIVE_READINESS.md](LIVE_READINESS.md).

Aucune transaction réelle n'a été exécutée. `TRADING_MODE=PAPER` reste la valeur par défaut et le code LIVE n'est pas déclaré prêt.
