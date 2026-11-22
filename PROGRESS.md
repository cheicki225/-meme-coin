# Progression vérifiable

Mise à jour : 22 novembre 2026. La progression mesure le périmètre de développement, pas la rentabilité, la disponibilité en production ni l'autorisation d'utiliser des fonds.

## Avancement pondéré

Chaque objectif compte de façon égale. Barème publié pour rendre l'estimation reproductible : `VERIFIED = 100`, `TESTED = 75`, `IMPLEMENTED = 50`, `IN PROGRESS = 25`, `NOT STARTED/BLOCKED = 0`. Un test local mocké ne vaut pas validation d'exploitation.

| # | Objectif | État | Score | Preuve et limite actuelle |
|---:|---|---|---:|---|
| 1 | Revue des 16 fichiers de la PR #3 | VERIFIED | 100 | Liste et constats consignés dans [AUDIT_COMPLET.md](AUDIT_COMPLET.md) |
| 2 | Sécurité Jupiter et transactions | TESTED | 75 | Ordres, montants, payeurs, transactions v0, ALT, ATA, frais et résultats confirmés testés localement; routes CPI/DEX non auditées indépendamment |
| 3 | Collecteur et bot | TESTED | 75 | Collecteur lecture seule, curseur/idempotence, panne RPC distincte d'un historique vide et mode sans clé Helius testés; pas de soak fournisseur |
| 4 | PnL réel et frais | TESTED | 75 | PnL LIVE lit les deltas/frais confirmés; frais PAPER d'entrée, DCA, sorties et Sell Initials intégrés/testés. Le cours USD n'est pas historisé |
| 5 | SQLite et reprise | TESTED | 75 | WAL, sauvegardes, import, restauration, signatures pendantes et erreurs SQLite fail-closed testés; aucune restauration en déploiement |
| 6 | RPC et erreurs | TESTED | 75 | Retry 429/5xx, `Retry-After`, jitter et journaux sans exception URL testés; charge, fraîcheur et endpoint déployé inconnus |
| 7 | Monitoring et Telegram | TESTED | 75 | Livraison/échec Telegram, cooldown RPC et retry d'alerte testés avec adaptateurs mockés; aucun envoi réel de déploiement vérifié |
| 8 | Backtesting | IN PROGRESS | 25 | Replay OHLC frais/slippage et vérification optionnelle des intervalles/gaps existent; intégration stratégie, archive et provenance restent absentes |
| 9 | Tests automatisés | TESTED | 75 | 196 tests locaux réussis le 22 novembre 2026; CI complète de cette révision à confirmer sur GitHub |
| 10 | Pertes, exposition, arrêt d'urgence | TESTED | 75 | Garde d'exposition, réservations et blocage fail-closed testés; en LIVE l'arrêt bloque achats et ventes sans liquider; aucun soak PAPER |

**Avancement pondéré : 72,5 %** (`725 / 10`). La valeur est une estimation de maturité d'implémentation selon le barème ci-dessus; ce n'est pas un taux de fonctionnalités prêtes pour des fonds.

## État de préparation par contexte

- **Développement : 72,5 %**, score pondéré documenté ci-dessus.
- **Tests locaux :** compilation réussie et **196 tests réussis** avec `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`; le taux de couverture lignes/branches n'a pas été mesuré.
- **Sécurité : IN PROGRESS.** Correctifs et tests locaux présents; CI dépendance/secrets de cette révision à confirmer. Aucun rapport d'audit indépendant des programmes CPI/DEX.
- **PAPER : IN PROGRESS.** C'est le mode par défaut; frais simulés intégrés et testés. Il reste à faire un soak et une restauration dans l'environnement cible.
- **LIVE : BLOCKED / NOT READY.** Aucune transaction réelle exécutée; pas d'intégration mainnet, soak, restauration en production ou revue indépendante complète. Ne pas activer les deux opt-ins LIVE.

## Hypothèses non vérifiées

Le code permet l'HTTP Solana public par défaut, mais le point d'accès réellement utilisé au déploiement est **UNKNOWN**. Aucun fichier `.env`, secret, tableau de bord fournisseur ni journal privé n'a été lu. Sans clé Helius, le mode PAPER démarre en capacité dégradée : les listeners WebSocket et les transactions Helius Enhanced ne sont pas disponibles.

Voir [PROJECT_ROADMAP.md](PROJECT_ROADMAP.md), [TESTING.md](TESTING.md) et [LIVE_READINESS.md](LIVE_READINESS.md).
