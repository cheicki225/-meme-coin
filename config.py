"""
════════════════════════════════════════════════════════════════
CONFIGURATION — Sniper Bot Solana (méthode dev-tracing)
════════════════════════════════════════════════════════════════
Toutes les valeurs sensibles viennent des variables d'environnement.
Ne JAMAIS mettre de clé API ou de clé privée en dur ici.
"""

import os

# ── Chargement automatique du fichier .env s'il existe (dev local) ─
# Sur Railway/production, les variables d'environnement sont déjà injectées
# directement par la plateforme — python-dotenv ne fait rien dans ce cas.
try:
    from dotenv import load_dotenv
    # encoding="utf-8-sig" gère un éventuel BOM en tête de fichier (ajouté par
    # certains éditeurs comme VS Code sur Windows) sans planter ni fausser le
    # nom de la première variable lue.
    load_dotenv(encoding="utf-8-sig")
except ImportError:
    pass

# ── Mode de fonctionnement ──────────────────────────────────────
# PAPER = simulation complète, aucun ordre réel envoyé (défaut, toujours garder ainsi
#         tant que le bot n'a pas été validé sur plusieurs semaines)
# LIVE  = exécution réelle via Jupiter (à activer uniquement plus tard)
TRADING_MODE = os.getenv("TRADING_MODE", "PAPER")

# ── Accès RPC / WebSocket Solana ────────────────────────────────
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else "https://api.mainnet-beta.solana.com"
HELIUS_WS_URL = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else None
# API "Enhanced Transactions" de Helius : parse déjà les instructions Pump.fun
HELIUS_PARSE_TX_URL = f"https://api.helius.xyz/v0/transactions/?api-key={HELIUS_API_KEY}"

# ── RPC de secours (Alchemy) — utilisé automatiquement par execution/rpc_client.py
# si Helius échoue/timeout. Laisse ALCHEMY_API_KEY vide pour désactiver (Helius seul).
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY", "")
ALCHEMY_RPC_URL = f"https://solana-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}" if ALCHEMY_API_KEY else None

# ── Sécurité token (GoPlus — gratuit, clé optionnelle) ────────────
# Activation par wallet via settings["security_check_enabled"] (voir DEFAULT_WALLET_SETTINGS).
GOPLUS_API_KEY = os.getenv("GOPLUS_API_KEY", "")
GOPLUS_API_SECRET = os.getenv("GOPLUS_API_SECRET", "")

# ── Mobula — identification d'adresses (exchanges, labels comportementaux) ─
# Clé API gratuite en libre-service immédiat : https://admin.mobula.io
# (contrairement à Arkham, jamais confirmé gratuit et à accès sur demande).
MOBULA_API_KEY = os.getenv("MOBULA_API_KEY", "")

# ── GMGN — analyse enrichie de wallet (PnL, win rate, classification) ────
# AJOUTÉ suite à une demande explicite, 19 août. Clé API GMGN, lecture
# seule (endpoints "Exist auth" uniquement — voir gmgn_client.py).
# Générée via gmgn-cli en LOCAL (paire de clés Ed25519), la clé API elle-
# même doit être ajoutée ICI (variable d'environnement Railway), séparément
# du fichier ~/.config/gmgn/.env du CLI local — deux environnements
# différents, la clé doit être configurée dans les deux si tu veux utiliser
# le CLI en local ET le bot en ligne.
# ⚠️ GMGN_PRIVATE_KEY n'est PAS demandée ici et ne doit jamais l'être —
# reste exclusivement dans le fichier local du CLI, jamais sur Railway.
GMGN_API_KEY = os.getenv("GMGN_API_KEY", "")

# ── Vérification des résultats Mobula (wallet/deployer) ──────────────────
# True (défaut) : chaque résultat Mobula est confirmé sur la blockchain
# (1-2 appels Helius légers) avant d'être accepté — le plus fiable.
# False : fait confiance à Mobula directement, ZÉRO appel Helius pour cette
# étape — plus rapide et économe en quota, mais aucune protection si Mobula
# se trompe (testé : peut arriver, voir le bug dict/string du 14 août).
# Mis à False temporairement pour tester Mobula seul — repasser à True si
# ça montre des faux positifs.
MOBULA_VERIFY_ON_CHAIN = os.getenv("MOBULA_VERIFY_ON_CHAIN", "false").lower() == "true"

# ── Solscan — alternative à Helius pour la recherche de transferts fixes ─
# Palier gratuit officiel : 10M CU/mois, 1000 req/60s (largement plus
# généreux que ce qui posait problème avec Helius sur pattern_detector.py).
# ⚠️ Header d'authentification non confirmé avec certitude absolue via la
# doc publique — "token" est la convention la plus courante, à valider au
# premier vrai test (voir solscan_client.py).
SOLSCAN_API_KEY = os.getenv("SOLSCAN_API_KEY", "")
SOLSCAN_API_BASE_URL = "https://pro-api.solscan.io/v2.0"

# ── IA d'appui au scoring (Claude ou Grok — Claude en priorité si les deux sont configurées) ─
# Purement advisory (voir main.py — un avis IA négatif n'annule jamais seul une décision).
# Activation par wallet via settings["ai_advisor_enabled"].
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GROK_API_KEY = os.getenv("GROK_API_KEY", "")
AI_MODEL_ANTHROPIC = os.getenv("AI_MODEL_ANTHROPIC", "claude-haiku-4-5-20251001")
AI_MODEL_GROK = os.getenv("AI_MODEL_GROK", "grok-4-fast")  # nom de modèle à vérifier, change souvent chez xAI

# ── Exécution LIVE (Jupiter) ─────────────────────────────────────
# SOLANA_PRIVATE_KEY : clé privée base58 du wallet d'exécution (comme affichée
# par Phantom "Export Private Key"). JAMAIS logguée, JAMAIS affichée dans Telegram.
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "")

# ── Multi-wallet géré depuis le bot (wallet_manager.py) ──────────
# Clé de chiffrement des wallets créés/importés depuis Telegram. Génère-la
# une seule fois avec wallet_manager.generate_encryption_key() et colle-la
# dans .env — sans elle, les wallets stockés deviennent illisibles.
WALLET_ENCRYPTION_KEY = os.getenv("WALLET_ENCRYPTION_KEY", "")
SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"
JUPITER_PRIORITIZATION_FEE_LAMPORTS = int(os.getenv("JUPITER_PRIORITIZATION_FEE_LAMPORTS", "100000"))  # ~0.0001 SOL

# ── Jito (bundles + tips) — vraie amélioration de vitesse d'inclusion ─────
# Vérifié via recherche web (docs Jito, mi-2026) : 95%+ du stake actif Solana
# tourne le client Jito-Solana, donc payer un tip via bundle a un vrai impact
# sur la vitesse d'inclusion. Les comptes de tip sont récupérés DYNAMIQUEMENT
# via l'endpoint getTipAccounts (voir jito_client.py) plutôt que codés en dur
# — une adresse figée peut devenir invalide/obsolète.
JITO_ENABLED = os.getenv("JITO_ENABLED", "false").lower() == "true"
JITO_TIP_LAMPORTS = int(os.getenv("JITO_TIP_LAMPORTS", "100000"))  # à monter si tu veux prioriser la vitesse
JITO_BLOCK_ENGINE_URL = os.getenv("JITO_BLOCK_ENGINE_URL", "https://mainnet.block-engine.jito.wtf/api/v1/bundles")
# Endpoints régionaux alternatifs (potentiellement plus rapides selon ta localisation) :
# amsterdam.mainnet.block-engine.jito.wtf / frankfurt... / ny... / tokyo... / slc...
TX_CONFIRMATION_TIMEOUT_S = int(os.getenv("TX_CONFIRMATION_TIMEOUT_S", "60"))

# ── Programme Pump.fun (adresse publique du programme on-chain) ─
PUMP_FUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# ── Telegram ─────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# ── Bannière affichée au /start (image locale, voir telegram_bot.py cmd_start) ─
# Place l'image de bannière dans le dossier du bot et indique son nom ici.
BANNER_IMAGE_PATH = os.getenv("BANNER_IMAGE_PATH", "banner.jpg")
DATA_FILE = os.getenv("DATA_FILE", "sniper_data.json")

# ── Filtres de découverte des tokens (étape 1 de la vidéo) ──────
VOLUME_MIN_USD = float(os.getenv("VOLUME_MIN_USD", "30000"))
DEV_CREATED_MAX = int(os.getenv("DEV_CREATED_MAX", "10"))  # max de tokens déjà créés par le dev

# ── Paramètres de trading simulé ─────────────────────────────────
TP_PCT = float(os.getenv("TP_PCT", "100"))        # take profit à +100%
# Stop loss désactivé par défaut (0 = OFF, demande explicite). paper_trader.py
# (ligne ~472 : "if sl_pct and change_pct <= -sl_pct") saute déjà proprement le
# check si sl_pct est falsy — donc 0 désactive vraiment le SL, pas de vente
# forcée à -0%. Repasser à une valeur comme "30" (ou éditer via le bouton
# "✏️ SL %" du menu wallet Telegram) pour le réactiver, globalement ou par wallet.
SL_PCT = float(os.getenv("SL_PCT", "0"))          # 0 = SL désactivé par défaut
POSITION_SIZE_USD = float(os.getenv("POSITION_SIZE_USD", "50"))
BUDGET_USD = float(os.getenv("BUDGET_USD", "500"))
CIRCUIT_BREAKER_USD = float(os.getenv("CIRCUIT_BREAKER_USD", "150"))

# ── Backtest / validation avant ajout au monitoring ──────────────
BACKTEST_MIN_TOKENS = int(os.getenv("BACKTEST_MIN_TOKENS", "10"))  # nb de tokens passés à analyser
BACKTEST_MIN_RATIO = float(os.getenv("BACKTEST_MIN_RATIO", "3.0"))  # ratio gain/perte minimum (3 pour 1 comme dans la vidéo)

# AJOUTÉ (demande explicite, 19 août) : les 4 critères de qualité utilisés
# par main._evaluate_new_dev pour juger un nouveau dev auto-détecté —
# avant un mélange de constantes ci-dessus (BACKTEST_MIN_RATIO,
# MAX_FIRST_CANDLE_MARKET_CAP plus bas) et de valeurs codées EN DUR
# directement dans main.py (le "3" tokens minimum, le "0.3" de régularité).
# Regroupés ici comme DÉFAUT INITIAL uniquement — une fois le bot démarré,
# c'est state["auto_detection_settings"] (monitoring_list.py) qui fait foi,
# modifiable depuis Telegram (menu Settings → Détection auto). Ces
# constantes ne servent donc plus qu'à initialiser un nouveau state vierge.
DEFAULT_AUTO_DETECTION_SETTINGS = {
    "filters_enabled": True,   # interrupteur général — False = ajoute tout nouveau dev sans AUCUN filtre
    "min_tokens_created": 3,   # historique minimum pour juger (était codé en dur "3" dans main.py)
    "min_ratio": 3.0,          # ratio gain/perte minimum (reprend BACKTEST_MIN_RATIO comme défaut)
    "min_regularity": 0.3,     # régularité de vente minimum (était codé en dur "0.3" dans main.py)
    "max_bundle_usd": 15000,   # market cap max de la 1ère bougie avant d'exclure comme "bundle" (reprend MAX_FIRST_CANDLE_MARKET_CAP)
    # AJOUTÉ (demande explicite, 19 août) : distinct de "filters_enabled" —
    # celui-ci contrôle si le pipeline d'évaluation de NOUVEAUX devs (Cas 2
    # dans main.on_new_token) tourne DU TOUT. False = achète UNIQUEMENT les
    # créations des Ruggeurs déjà dans ta liste (Cas 1, jamais affecté par
    # ce réglage) — aucun nouveau dev inconnu n'est jamais ajouté ni acheté,
    # peu importe son historique. "filters_enabled=False" fait l'INVERSE
    # (accepte tout le monde sans filtre) — les deux réglages sont
    # indépendants, ne pas les confondre.
    "auto_add_new_devs": True,
}

# ── Détection de "montant fixe" pour le schéma exchange ──────────
FIXED_AMOUNT_TOLERANCE_SOL = float(os.getenv("FIXED_AMOUNT_TOLERANCE_SOL", "0.0005"))

# ── Adresses communes des exchanges connues (pour détecter le schéma "exchange") ─
# Liste non-exhaustive à enrichir au fil du temps — ce sont les "hot wallets" publics
KNOWN_EXCHANGE_ADDRESSES = {
    "Binance":  ["9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"],
    "Bybit":    ["AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2"],
    "Coinbase": ["H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS"],
    "OKX":      ["5VCwKtCXgCJ6kit5FybXjvriW3xELsFDhYrPSqtJNmcD"],
}

# ── Réglages par défaut d'un wallet nouvellement ajouté au monitoring ─
# Reproduit les "settings par défaut" de F Project (vu dans la vidéo de démo live).
# Chaque wallet peut ensuite override ces valeurs individuellement (voir monitoring_list.py).
DEFAULT_WALLET_SETTINGS = {
    "buy_amount_sol": float(os.getenv("DEFAULT_BUY_AMOUNT_SOL", "0.1")),
    # Vente échelonnée : liste de {"pct": seuil de gain, "sell_ratio": fraction de la position à vendre}
    "tp_levels": [{"pct": TP_PCT, "sell_ratio": 1.0}],
    "sl_pct": SL_PCT,
    "min_market_cap": None,   # protection : n'achète pas en dessous de ce market cap
    "max_market_cap": 6500,  # protection : n'achète pas au-dessus (par défaut demandé pour tous les nouveaux wallets)
    "snipe_delay_s": 0,       # délai avant achat après détection (0 = immédiat, bloc zéro)
    "no_activity_sell_s": 35,  # vend si aucune activité (achat/vente) pendant N secondes — SL "naturel"
    "buy_only_once": True,    # MODIFIÉ (19 août) : bloque le RACHAT du même token, plus jamais tout le wallet — voir monitoring_list.has_already_bought
    # AJOUTÉ (demande explicite) : n'achète en copy trade que si le token a
    # moins de N secondes au moment de l'achat détecté — filtre les copy
    # trades sur des tokens qui traînent déjà depuis un moment, pas des
    # snipes frais. None ou 0 désactive le filtre. Voir
    # wallet_history.get_token_creation_time et paper_trader.open_position.
    "max_token_age_at_buy_s": 15,
    "trailing_sl_enabled": False,
    "buy_on_dev_sell": False,  # stub — racheter après la vente partielle du dev (non implémenté)
    # ── Ajouts F Project (menu Ruggers / Config par Rugger) ──
    "auto_buy": True,
    "auto_sell": True,
    "max_consecutive_losses": 3,   # coupe l'auto-buy après N pertes d'affilée (0 = désactivé)
    "protection_enabled": True,
    "protection_mode": "last_transfer",  # "last_transfer" ou "exchange_pattern"
    "fresh_wallet_only": False,          # n'ajoute que des wallets jamais actifs
    "transfer_ranges": [],               # jusqu'à 3 plages {"min":x,"max":y} — override le montant fixe unique
    "keep_address": False,               # garde l'ancienne adresse en plus de la nouvelle
    "inherit_settings": True,            # les adresses enfants héritent des settings du parent
    "front_run_sell_enabled": False,     # BETA — voir execution/protection_scanner.py pour le niveau d'implémentation réel
    "front_run_sell_threshold_pct": 50,
    "consecutive_losses": 0,             # compteur courant (géré par le bot, pas par l'utilisateur)
    "paused": False,                     # mis à True par le Max Loss Counter
    # ── Buy Modes Simple/Hardcore ────────────────────────────────
    # En PAPER, aucune vraie course multi-serveurs n'a lieu (pas d'infra à répliquer).
    # On simule l'effet observable documenté : Hardcore a un taux d'échec de transaction
    # plus élevé (CU réduit) mais ne débite jamais en cas d'échec, et affiche des frais
    # simulés plus élevés. C'est cosmétique/pédagogique, pas une vraie course de blocs.
    "buy_mode": "simple",               # "simple" ou "hardcore"
    "hardcore_confirmed": False,        # l'utilisateur a vu et accepté l'écran d'avertissement
    # ── Buy The Dip (jusqu'à 3 paliers, DCA sous l'ATH) ──────────
    "buy_the_dip_levels": [],           # liste de {"drop_pct": float, "amount_sol": float, "triggered": bool}
    "ath_market_cap": None,             # géré par le bot, ATH observé depuis l'achat initial
    # ── Auto-Sell on Big Buy (jusqu'à 3 paliers) ─────────────────
    # Nécessite de monitorer les buys sur le token en position — voir la limite documentée
    # dans execution/paper_trader.py (résolution la plus fine disponible gratuitement).
    "auto_sell_big_buy_levels": [],     # liste de {"min_sol": float, "max_sol": float, "sell_ratio": float, "triggered": bool}
    # ── Trailing SL multi-niveaux par palier de market cap ───────
    "trailing_sl_tiers": [{"mc_threshold": 0, "trailing_pct": 20}],  # trié par mc_threshold croissant
    "trailing_ath_market_cap": None,    # géré par le bot
    # ── Sell Initials ─────────────────────────────────────────────
    "initials_sold": False,             # géré par le bot, une fois par position
    # ── Copytrade uniquement (Max Token Age / Follow Cooldown) ───
    "max_token_age_min": 0,             # 0 = OFF ; ignore les achats copiés sur un token plus vieux que N minutes
    "follow_cooldown_s": 0,             # 0 = OFF ; ignore les achats rapprochés du même rugger pendant N secondes
    "last_copy_trade_time": None,       # géré par le bot
    # ── Child Address Defaults (utilisés quand inherit_settings=False) ─
    "child_defaults": {
        "keep_address": False,
        "protection_enabled": False,
        "auto_buy": True,
        "auto_sell": True,
        "remove_if_no_launch_h": 2,     # "🧹 Remove if no launch" — probation en heures
    },
    # ── Sécurité GoPlus (avant achat) ─────────────────────────────
    "security_check_enabled": False,     # OFF par défaut : la bonding curve Pump.fun limite déjà le risque
    "min_security_score": 60,            # score GoPlus minimum (0-100) pour valider l'achat
    # ── Avis IA (Claude/Grok) — appui au scoring, jamais bloquant seul ─
    "ai_advisor_enabled": False,
    # ── Réglages d'exécution (menu "Snipe Config", comme F Project) ────
    # SIMULÉS en PAPER — aucune vraie transaction n'est envoyée, donc slippage,
    # frais de gaz, tip et anti-MEV n'ont aucun effet réel sur l'exécution.
    # Exposés quand même pour que l'interface se rapproche de F Project et pour
    # préparer le passage éventuel en LIVE (où ils deviendraient vrais).
    "gas_fee_sol": 0.0025,
    "snipe_tip_sol": 0.005,
    "anti_mev_enabled": False,
    "buy_slippage_pct": 50,
    "buy_slippage_pump_pct": 50,
    "platforms": ["pump_fun"],  # Pump.fun est la seule plateforme réellement détectée pour l'instant
    # ── Filtres d'analyse RÉELS sur le comportement du dev ──────────────
    # min/max SOL dépensés par le dev lui-même sur son propre token à l'achat
    # (approximé via son solde de tokens juste après détection — voir paper_trader.py)
    "dev_buy_min_sol": None,
    "dev_buy_max_sol": None,
    # % de la supply totale que le dev détient encore — filtre anti-rug supplémentaire
    "dev_holding_min_pct": None,
    "dev_holding_max_pct": None,
    # ── MultiBuy — split l'achat sur plusieurs wallets gérés (wallet_manager.py) ─
    # Réduit le price impact / la détectabilité, comme montré dans les vidéos
    # ("acheter 600-700$ répartis plutôt que 2000$ d'un coup").
    "multibuy_enabled": False,
    "multibuy_wallet_labels": [],   # labels de wallets gérés à utiliser (voir wallet_manager.py)
    "multibuy_delay_s": 3,          # délai entre chaque sous-achat
    # ── Priorité vitesse — désactive certaines protections pour gagner du temps ─
    # skip_price_check_for_speed=True saute l'appel DexScreener de pré-achat
    # (utilisé pour les filtres min/max market cap et détention dev) — un vrai
    # aller-retour réseau économisé, mais ces protections ne s'appliquent plus.
    "skip_price_check_for_speed": False,
    "use_jito": False,  # override par wallet du JITO_ENABLED global (LIVE uniquement)
    # ── Achat sur pullback avec timeout ──────────────────────────────
    # Interdit d'acheter au-dessus d'un market cap donné ; si le prix vient
    # de dépasser ce seuil, attend qu'il redescende dessous (ou dessus, selon
    # le niveau visé) pendant un délai max, sinon abandonne ce token.
    "pullback_entry_enabled": False,
    "pullback_max_mc": 4500,       # market cap max pour acheter (attend la descente si dépassé)
    "pullback_timeout_s": 5,       # délai max d'attente avant d'abandonner ce token
    # ── Vente automatique si repli sous un market cap donné après un pic ─
    # Une fois que le token a atteint mc_trailing_arm_threshold au moins une
    # fois, si le market cap retombe à mc_trailing_sell_threshold, vente totale.
    "mc_trailing_enabled": False,
    "mc_trailing_arm_threshold": 5000,   # MC à atteindre pour "armer" la protection
    "mc_trailing_sell_threshold": 2500,  # MC de vente une fois armé
    # ── Trailing stop sur le % de gain (breakeven progressif) ────────────
    # MODIFIÉ (demande explicite, 19 août) : avant, le plancher restait figé
    # à profit_trail_initial_floor_pct jusqu'à ce que le gain atteigne
    # profit_trail_tight_arm_pct (2 étapes distinctes). Maintenant, dès
    # l'armement (gain >= profit_trail_arm_pct), le plancher trail EN
    # CONTINU : plancher = max(profit_trail_initial_floor_pct, pic de gain -
    # profit_trail_gap_pct) — remonté à chaque nouveau sommet, jamais
    # abaissé. profit_trail_tight_arm_pct n'est plus utilisé (gardé dans les
    # settings existants pour compatibilité, sans effet).
    "profit_trail_enabled": True,
    "profit_trail_arm_pct": 50,
    "profit_trail_initial_floor_pct": 20,
    "profit_trail_tight_arm_pct": 105,  # DÉPRÉCIÉ — plus utilisé, voir commentaire ci-dessus
    "profit_trail_gap_pct": 30,
    # AJOUTÉ (demande explicite, 19 août — passé de global à individuel par
    # wallet) : liste de devs à éviter pour CE wallet Copy Trading précis.
    # Indépendante du monitoring — un dev bloqué n'a pas besoin d'être un
    # wallet suivi par le bot. Voir monitoring_list.add_blocked_dev.
    "blocked_devs": {},                 # {address: {label, added_at}}
}

# ── Referral (simulé — pas de vrai système de paiement/commission) ─
DEFAULT_REFERRAL_STATE = {
    "code": None,          # généré à la demande, voir monitoring_list.generate_referral_code()
    "referred_count": 0,   # purement déclaratif, jamais peuplé automatiquement
    "commission_pct": 35,
}

# ── Toggles globaux (page Ruggers) ────────────────────────────
GLOBAL_AUTO_BUY_DEFAULT = True

# ── Scan permanent Pump.fun (détection + évaluation automatique) ─────────
# Désactivé temporairement à la demande — le reste du bot (Telegram, Copy
# Trading, Protection scanner) continue de fonctionner normalement. Remets
# à True dans .env (DETECTION_ENABLED=true) quand tu veux réactiver la
# découverte automatique de nouveaux ruggers.
DETECTION_ENABLED = os.getenv("DETECTION_ENABLED", "false").lower() == "true"
GLOBAL_AUTO_SELL_DEFAULT = True

# ── Slots maximum de wallets monitorés simultanément (comme F Project : 30) ─
MAX_MONITORED_WALLETS = int(os.getenv("MAX_MONITORED_WALLETS", "30"))

# ── Préférences de notifications par défaut (menu Settings → Notifications) ─
# buy_failed / sell_failed / bot_errors sont toujours ON, non désactivables (voir telegram_bot.py)
DEFAULT_NOTIFICATION_PREFS = {
    "buy_confirmed": True,
    "sell_success": True,
    "rugger_alert": True,
    "buy_skipped": False,
    "processing_buy": False,
    "rug_scan_alert": True,  # AJOUTÉ avec le scanner de rugs complet (rug_scanner.py)
    # AJOUTÉ (demande explicite, 19 août) : catégorie DÉDIÉE pour les
    # alertes de retrait ("Retrait SOL/USDC détecté" + "Transfert SOL
    # important détecté" / 90% du solde). Avant, ces deux-là partageaient
    # "rugger_alert" avec des alertes SANS RAPPORT (nouveau wallet
    # découvert via Protection, avis IA sur un dev auto-ajouté) — les
    # couper ensemble aurait été un tout-ou-rien imprécis. Voir
    # main.on_wallet_withdrawal et main.on_dev_transfer_alert (ou
    # équivalent) pour les deux appels concernés.
    "withdrawal_alert": True,
}

# ── Filtre de backtest : rejette les tokens dont la 1ère bougie/bundle est trop haute ─
# ("pas de bundle à plus de 15k" — vidéo copy trading gratuit)
MAX_FIRST_CANDLE_MARKET_CAP = float(os.getenv("MAX_FIRST_CANDLE_MARKET_CAP", "15000"))

# ── Surveillance proactive des schémas de transfert (menu "Protection" de F Project) ─
# Intervalle de scan des adresses "mère" ou "exchange" surveillées pour repérer
# automatiquement les nouveaux wallets financés, AVANT qu'ils créent un token.
PROTECTION_SCAN_INTERVAL_S = int(os.getenv("PROTECTION_SCAN_INTERVAL_S", "10"))  # 10s par défaut (demande explicite — était 60s ; augmente la charge RPC, voir avertissement dans le code)

# ── Scanner de rugs — TOUS les nouveaux tokens Pump.fun (demande explicite) ──
# Contrairement au pipeline d'évaluation existant (historique, ratio backtest,
# régularité), ce scanner filtre TOUS les tokens détectés selon 4 critères
# simples. Dépend du MÊME listener WebSocket que le pipeline principal —
# donc nécessite DETECTION_ENABLED=true pour recevoir des tokens à évaluer
# (pas de deuxième connexion WebSocket dédiée, pour ne pas doubler la charge).
RUG_SCAN_ENABLED = os.getenv("RUG_SCAN_ENABLED", "false").lower() == "true"
RUG_SCAN_INTERVAL_S = int(os.getenv("RUG_SCAN_INTERVAL_S", "60"))          # fréquence de re-vérification des candidats
RUG_SCAN_MIN_VOLUME_USD = float(os.getenv("RUG_SCAN_MIN_VOLUME_USD", "20000"))
RUG_SCAN_MAX_DEV_CREATIONS = int(os.getenv("RUG_SCAN_MAX_DEV_CREATIONS", "3"))
RUG_SCAN_MAX_AGE_HOURS = float(os.getenv("RUG_SCAN_MAX_AGE_HOURS", "24"))
RUG_SCAN_MIN_TX_COUNT = int(os.getenv("RUG_SCAN_MIN_TX_COUNT", "250"))

# ── Copy Trading — fréquence de synchronisation des wallets suivis ──
# CORRIGÉ : était codé en dur à 30s dans copytrade_listener.py, pas
# configurable comme le reste des intervalles du projet.
COPYTRADE_SYNC_INTERVAL_S = int(os.getenv("COPYTRADE_SYNC_INTERVAL_S", "10"))  # 10s par défaut (demande explicite — était 30s fixe)

# ── Détection de transfert SOL important d'un dev surveillé ──
# AJOUTÉ suite à une demande explicite : détecte quand un dev en mode
# Ruggeur (track_creation) transfère une grosse partie de son solde SOL
# vers une autre adresse — signal fort qu'il encaisse et se prépare à
# disparaître, distinct d'une simple vente de token sur la bonding curve.
DEV_SOL_TRANSFER_ALERT_PCT = float(os.getenv("DEV_SOL_TRANSFER_ALERT_PCT", "90"))  # % du solde transféré qui déclenche l'alerte

# ── Alerte retrait SOL (montant absolu, tous wallets surveillés) ──
# AJOUTÉ suite à une demande explicite — DIFFÉRENT du seuil DEV_SOL_TRANSFER_ALERT_PCT
# ci-dessus (qui ne concerne que les devs, en % du solde). Ici : n'importe
# quel wallet surveillé (Ruggeur OU Copy Trading) qui envoie du SOL vers une
# autre adresse, au-delà d'un montant ABSOLU minimum — pas de vérification
# de solde, juste "a-t-il envoyé au moins X SOL ?".
WITHDRAWAL_ALERT_ENABLED = os.getenv("WITHDRAWAL_ALERT_ENABLED", "true").lower() == "true"
WITHDRAWAL_ALERT_MIN_SOL = float(os.getenv("WITHDRAWAL_ALERT_MIN_SOL", "0.1"))
# AJOUTÉ suite à une demande explicite : détection étendue à l'USDC (un
# token SPL, pas du SOL natif — nécessite un seuil séparé, en dollars
# puisque 1 USDC ≈ 1$).
WITHDRAWAL_ALERT_MIN_USDC = float(os.getenv("WITHDRAWAL_ALERT_MIN_USDC", "20"))
# AJOUTÉ suite à un vrai effet en cascade observé : une adresse ajoutée
# automatiquement (via cette alerte) pouvait elle-même déclencher un
# nouvel ajout automatique si ELLE faisait aussi un retrait, sans limite —
# risque de remplir les 30 wallets rapidement avec des adresses de plus en
# plus éloignées du signal de départ.
WITHDRAWAL_ALERT_AUTO_ADD = os.getenv("WITHDRAWAL_ALERT_AUTO_ADD", "true").lower() == "true"  # ajout auto ON/OFF (sinon juste notifier)
WITHDRAWAL_ALERT_ALLOW_CASCADE = os.getenv("WITHDRAWAL_ALERT_ALLOW_CASCADE", "false").lower() == "true"  # False = stoppe la chaîne à 1 niveau
# AJOUTÉ suite à un vrai cas observé : un SEUL wallet source très actif
# (visiblement un bot/service, ~84 SOL, 9 tokens, plusieurs retraits par
# minute) déclenchait plusieurs ajouts automatiques DIFFÉRENTS en quelques
# secondes — un scénario que le blocage de cascade ne couvre pas (ce n'est
# pas une adresse déjà auto-ajoutée qui recascade, c'est la MÊME source
# d'origine qui spamme). Limite à 1 ajout automatique par heure et par
# wallet SOURCE, peu importe combien de retraits il fait entre-temps.
AUTO_ADD_COOLDOWN_S = int(os.getenv("AUTO_ADD_COOLDOWN_S", "3600"))

DEV_TRANSFER_AUTO_ADD = os.getenv("DEV_TRANSFER_AUTO_ADD", "true").lower() == "true"
DEV_TRANSFER_ALLOW_CASCADE = os.getenv("DEV_TRANSFER_ALLOW_CASCADE", "false").lower() == "true"

# ── Nettoyage automatique des wallets surveillés ──
# AJOUTÉ suite à une demande explicite : retire du monitoring les wallets
# inactifs depuis trop longtemps, ou ayant enchaîné trop de pertes
# consécutives. Réglable directement depuis Telegram (voir
# monitoring_list.get_cleanup_settings/set_cleanup_settings) — ces valeurs
# ne servent que de défaut initial.
WALLET_CLEANUP_ENABLED = os.getenv("WALLET_CLEANUP_ENABLED", "true").lower() == "true"
WALLET_CLEANUP_INTERVAL_S = int(os.getenv("WALLET_CLEANUP_INTERVAL_S", "14400"))  # MODIFIÉ (demande explicite, 19 août) : 1h -> 4h, réduction de coût RPC
WALLET_CLEANUP_INACTIVE_DAYS = float(os.getenv("WALLET_CLEANUP_INACTIVE_DAYS", "30"))
WALLET_CLEANUP_MAX_CONSECUTIVE_LOSSES = int(os.getenv("WALLET_CLEANUP_MAX_CONSECUTIVE_LOSSES", "7"))
# AJOUTÉ (demande explicite, réduction du coût RPC) : profondeur de
# décodage on-chain utilisée par _count_consecutive_losses pour reconstruire
# le résultat de chaque trade analysé — voir backtest.get_detailed_trade_info
# (paramètre max_transactions). Réduit de 150 (valeur par défaut, utilisée
# pour "Analyse de wallet"/"Analyse de dev", déclenchées manuellement) à 40
# ici : ce check n'a besoin que de savoir si un trade est gagnant ou perdant
# (result_pct < 0), pas de sa magnitude exacte — 40 transactions restent
# largement suffisantes pour ça, pour une fraction du coût RPC. Avec 28
# wallets × jusqu'à 10 trades dans le pire cas (série de pertes), ça passe
# le plafond théorique d'environ 43 000 appels RPC par passage à environ
# 11 500 — une réduction d'environ 73%.
WALLET_CLEANUP_MAX_TRANSACTIONS_SCANNED = int(os.getenv("WALLET_CLEANUP_MAX_TRANSACTIONS_SCANNED", "20"))  # MODIFIÉ (demande explicite, 19 août) : 40 -> 20, réduction de coût RPC

# ── Buy on Dev Sell — délai max d'attente de la vente du dev après création ─
BUY_ON_DEV_SELL_TIMEOUT_MIN = float(os.getenv("BUY_ON_DEV_SELL_TIMEOUT_MIN", "30"))

# ── Détection "repérage" — alerte si un trade se solde en perte anormalement vite ─
# Si une position se clôture en SL (ou proche) en moins de ce délai, c'est un
# signal possible que le rugger a changé de comportement ou nous a repérés
# (comme décrit dans les vidéos : "si le mec vend juste après toi, supprime
# son adresse"). N'agit jamais automatiquement — notifie seulement, la décision
# de supprimer le wallet reste manuelle.
DETECTION_ALERT_TIME_THRESHOLD_S = int(os.getenv("DETECTION_ALERT_TIME_THRESHOLD_S", "20"))

# ── Taux de change indicatif SOL/USD — REPLI DE DERNIER RECOURS UNIQUEMENT ──
# CORRIGÉ suite à un vrai bug trouvé : cette constante statique était utilisée
# PARTOUT comme source principale de conversion SOL→USD, alors qu'elle n'était
# jamais mise à jour (150$ par défaut, pendant que le SOL réel valait ~76$ au
# moment du diagnostic — un écart de ~2x qui faussait market caps, coûts de
# base et PnL affichés). Toutes les conversions passent maintenant par
# backtest.get_sol_usd_rate() (taux en direct via DexScreener, mis en cache
# 5 min). Cette constante ne sert plus QUE de dernier repli si DexScreener est
# injoignable ET qu'aucune valeur n'a jamais été mise en cache (ex: tout
# premier appel juste après un redémarrage) — la garder à jour manuellement
# de temps en temps reste utile pour ce cas limite, mais n'affecte plus le
# fonctionnement normal du bot.
SOL_USD_RATE = float(os.getenv("SOL_USD_RATE", "150"))

# ── Logging ────────────────────────────────────────────────────
LOG_FILE = os.getenv("LOG_FILE", "sniper_bot.log")

# ── Rappel de sécurité ────────────────────────────────────────────
if TRADING_MODE == "LIVE":
    print("⚠️  ATTENTION : TRADING_MODE=LIVE — de l'argent réel sera engagé.")
    if not SOLANA_PRIVATE_KEY:
        print("❌ TRADING_MODE=LIVE mais SOLANA_PRIVATE_KEY est vide — le bot ne pourra pas exécuter d'ordres.")
