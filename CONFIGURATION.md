# Configuration

Les paramètres viennent de l'environnement ou d'un fichier `.env` local. Gardez `TRADING_MODE=PAPER`; c'est la valeur par défaut.

## Accès et clés

- RPC Solana : `HELIUS_API_KEY`; `ALCHEMY_API_KEY` est un fournisseur de secours facultatif.
- Telegram : `BOT_TOKEN` et `TELEGRAM_ALLOWED_USER_IDS` (identifiants numériques séparés par des virgules). Une allowlist vide refuse les commandes et callbacks.
- Wallet : `SOLANA_PRIVATE_KEY` est seulement nécessaire au code LIVE; `WALLET_ENCRYPTION_KEY` protège les wallets gérés par Telegram. Ne versionnez jamais ces secrets.
- Jupiter : `JUPITER_API_KEY` est envoyé uniquement aux endpoints officiels codés en dur `api.jup.ag/swap/v2/order` et `/execute`.

## Persistance et collecte

- `DATA_BACKEND=sqlite` par défaut; `SQLITE_DB_FILE` choisit le fichier d'état WAL. Un ancien `DATA_FILE=sniper_data.json` est importé au premier démarrage si la base n'a pas encore d'état.
- `WALLET_COLLECTOR_ENABLED=false` par défaut. S'il est activé, le collecteur utilise des appels RPC en lecture seule et stocke l'activité dans `WALLET_ACTIVITY_DB_FILE`.
- `WALLET_COLLECTOR_INTERVAL_S`, `WALLET_COLLECTOR_SIGNATURES_PER_WALLET` et `WALLET_COLLECTOR_MAX_WALLETS_PER_CYCLE` bornent la charge.

## Limites de risque

- `MAX_SLIPPAGE_BPS`, `MAX_PRICE_IMPACT_PCT`, `MAX_TRANSACTION_FEE_LAMPORTS` limitent les swaps.
- `MAX_JUPITER_RENT_LAMPORTS` plafonne le loyer estimé pour créer des comptes associés.
- `MAX_OPEN_EXPOSURE_USD` et `MAX_OPEN_POSITIONS` bornent l'exposition globale.
- `MAX_MANUAL_TRANSFER_SOL` borne les transferts manuels; ils sont bloqués en mode PAPER, pendant l'arrêt d'urgence ou lorsqu'une transaction est pendante.
- `CIRCUIT_BREAKER_USD` active l'arrêt d'urgence après les pertes configurées.

Les valeurs complètes sont décrites dans `config.py` et `.env.example`. L'exécution LIVE et les intégrations on-chain restent non validées; voir `SECURITY.md`.
