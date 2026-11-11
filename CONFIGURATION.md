# Configuration

Les paramètres sont lus depuis l'environnement ou `.env` local. `config.py` contient les valeurs par défaut. Le fichier `.env.example` répertorie les principales variables sans secrets.

- Données Solana : `HELIUS_API_KEY`, éventuellement `ALCHEMY_API_KEY`.
- Telegram : `BOT_TOKEN` et `TELEGRAM_ALLOWED_USER_IDS` (liste d'identifiants numériques séparés par des virgules).
- Exécution : `TRADING_MODE=PAPER` par défaut; clé facultative de wallet LIVE via `SOLANA_PRIVATE_KEY`.
- Wallets Telegram chiffrés : `WALLET_ENCRYPTION_KEY`, à sauvegarder hors du serveur.
- Protection d'exécution : `MAX_SLIPPAGE_BPS`, `MAX_PRICE_IMPACT_PCT`, `MAX_TRANSACTION_FEE_LAMPORTS`.

Les valeurs présentées dans `.env.example` ne remplacent pas toutes les options décrites dans `config.py`. N'ajoutez jamais de secret à un fichier versionné.
