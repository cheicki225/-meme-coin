# Audit du dépôt

## Périmètre

Audit statique de l'arborescence Python, configuration, exécuteur, portefeuille, persistance et Telegram. État observé : branche locale `main` au commit `e10cfaa`; 45 fichiers suivis à l'état initial, avec 19 fichiers Python modifiés et plusieurs nouveaux fichiers non suivis déjà présents. Ces modifications locales préexistantes sont conservées. Aucun audit formel on-chain ni test avec fonds n'a été effectué.

## Constats

| Gravité | Fichiers | Constat / action |
|---|---|---|
| Critique | `telegram_bot.py`, `config.py` | Les handlers Telegram n'avaient pas d'autorisation visible et `/start` enregistrait tout chat comme chat de notification. Une allowlist explicite d'utilisateurs privés est maintenant vérifiée avant les handlers. |
| Élevée | `jupiter_executor.py` | Le devis externe et la transaction étaient signés sans validation suffisante. Ajout de validations des mints, montants, slippage, impact, minimum reçu, payeur/signataires, programmes, blockhash et frais avant signature. |
| Élevée | `fund_tracer.py` | `_count_distinct_outgoing_recipients()` laisse l'ensemble des destinataires vide et retourne donc toujours 0; classification du schéma simple/mère non fiable. À corriger avant de s'appuyer sur ce signal. |
| Élevée | `live_trader.py`, `jupiter_executor.py` | Les timeouts de confirmation laissent un résultat incertain; la réconciliation des soldes et transactions avant retry n'est pas complète. N'exécutez pas de retry LIVE aveugle. |
| Élevée | `monitoring_list.py` | Persistance JSON locale sans verrou inter-processus ni écriture atomique visible; une interruption pendant sauvegarde peut corrompre l'état. |
| Moyenne | `backtest.py`, `wallet_history.py` | Le backtest emploie des données publiques approximatives, pas un historique OHLC fiable. Le score de régularité hérite de cette limite. |
| Moyenne | découverte, rug/copy trading | Les scores dépendent d'endpoints tiers et de la disponibilité de données historiques; manque de validation exhaustive face aux données absentes et comportements adversariaux. |
| Moyenne | dépôt | Pas de workflow CI ni de suite de tests dédiée identifiés à l'état initial; ajoutés dans cette mise à jour. |

## Corrections apportées

- Refus par défaut des événements Telegram si le compte n'est pas un utilisateur privé déclaré dans `TELEGRAM_ALLOWED_USER_IDS`.
- Validation des devis Jupiter et du message de transaction avant signature, validité du blockhash et plafond des frais vérifié via RPC.
- Exemple d'environnement sans secrets, documentation opérationnelle/sécurité et workflow CI.
- Tests unitaires pour les validations de devis.

## Problèmes ouverts

- Refaire `_count_distinct_outgoing_recipients()` avec extraction exacte et tests de transactions historiques.
- Persister les changements atomiquement, avec verrouillage, validation de schéma et réconciliation des signatures.
- Construire une réconciliation on-chain robuste avant toute nouvelle tentative après timeout.
- Couvrir par tests simulés l'exécution Jupiter, les ventes, les positions, le copy trading, le MultiBuy/Sell et les callbacks Telegram.
- Auditer chaque instruction compilée Solana et les transferts de SOL/jetons selon un modèle de menace formel. Les gardes présentes ne constituent pas une preuve que toute transaction tierce est sûre.
- Validation de bout en bout avec RPC et APIs actuels non réalisée; PAPER requis, LIVE non validé.

## État des phases

Audit statique : 100 %. Sécurité transactions/Telegram : 60 %. Persistance : 20 %. Trading, détection, scoring et copy trading : 35 %. Tests/CI : 30 %. Documentation : 70 %. Revue on-chain et préparation LIVE : 0 %. Ces estimations ne signifient pas que le produit est prêt à trader.
