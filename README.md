# Sniper Bot — Détection dev + traçage de fonds

Implémentation de la méthode "sniping de rugger" décrite dans la vidéo Faomo :
détection temps réel des créations de tokens Pump.fun, traçage du financement
du wallet dev, backtest automatique, et ajout au monitoring si rentable.

**Mode PAPER par défaut.** Le code LIVE existe, mais n'est pas validé pour des fonds réels. Il exige `TRADING_MODE=LIVE` et `LIVE_EXECUTION_ENABLED=true`; aucun swap réel n'a été exécuté pendant cette phase.

**Le mode LIVE n'est pas validé pour des fonds réels.** Les contrôles ajoutés ne remplacent pas un audit indépendant, les prix/API peuvent manquer ou être inexacts, et un timeout de transaction doit être réconcilié avant toute nouvelle tentative. Configurez `TELEGRAM_ALLOWED_USER_IDS` pour limiter l'accès Telegram. Voir [SECURITY.md](SECURITY.md), [INSTALLATION.md](INSTALLATION.md) et [AUDIT_COMPLET.md](AUDIT_COMPLET.md).

---

## ⚠️ À VÉRIFIER AVANT TOUTE UTILISATION — IMPORTANT

Ce code a été écrit sans accès à un environnement de test réel. Plusieurs
éléments sont **approximatifs ou à valider** avant de faire confiance aux résultats :

### 1. Adresses d'exchanges dans `config.py` (`KNOWN_EXCHANGE_ADDRESSES`)
**Ces adresses ne sont PAS vérifiées.** Il faut les confirmer toi-même sur
Solscan (comme dans la vidéo : chercher l'adresse commune, vérifier le badge
"Binance"/"Bybit" etc. sur la page). Une adresse fausse = détection silencieusement
cassée, sans erreur visible.

### 2. `PUMP_FUN_PROGRAM_ID` dans `config.py`
À reconfirmer sur Solscan/le site Pump.fun avant le premier lancement — les
program ID ne changent normalement pas, mais une erreur ici bloque toute
détection.

### 3. `_extract_creation_info()` dans `websocket_listener.py`
Le format exact de la réponse de l'API Enhanced Transactions de Helius pour
les événements Pump.fun peut avoir changé depuis. **Teste ce module seul
d'abord** (`python -m detection.websocket_listener`) et regarde le JSON brut
retourné avant de faire confiance à l'extraction automatique.

### 4. `check_sell_regularity()` dans `wallet_history.py`
**C'est un stub.** Il retourne un score neutre fixe (0.5) — la vraie logique
de la vidéo (le dev vend-il toujours au même point ?) n'est pas encore
implémentée, ça demande de croiser timestamps de création et de vente massive
par token. À construire en priorité si tu veux la fiabilité décrite dans
la vidéo.

### 5. `_count_distinct_outgoing_recipients()` dans `fund_tracer.py`
**C'est un stub incomplet** (retourne toujours 0 actuellement) — la distinction
schéma "simple" vs "mère" n'est pas fiable en l'état. Le schéma "exchange"
fonctionne correctement (basé sur les adresses connues).

### 6. `backtest_token()` dans `backtest.py`
Utilise l'API DexScreener gratuite qui ne donne **pas** un vrai historique
OHLC par bougie — j'approxime avec `priceChange.h24`. C'est une approximation
grossière du "on aurait acheté fin de 1ère bougie" de la vidéo, pas un vrai
backtest précis. Pour un backtest fiable il faudrait une source de données
avec OHLC minute par minute (Birdeye a une API payante pour ça, ou stocker
toi-même les prix au fil du temps une fois le bot en marche).

---

## Nouvelles fonctionnalités (basées sur F Project)

Reproduites à partir des transcripts de démonstration du bot F Project (sniping de dev uniquement, pas le copy trading) :

- **Surveillance proactive (`execution/protection_scanner.py`)** : au lieu d'évaluer un dev seulement après qu'il ait créé un token, le bot surveille en continu les adresses "mère" ou "exchange" (schémas de transfert) pour ajouter les nouveaux wallets financés **avant** leur première création. C'est ce qui permet un achat au "bloc zéro" plutôt qu'après coup. Configuration via `DataStore.add_protection_target()`.
- **Réglages par wallet** (`config.DEFAULT_WALLET_SETTINGS`, override par wallet) : `min/max_market_cap` (protection anti-anomalie), `snipe_delay_s`, `buy_only_once`, `no_activity_sell_s` (SL "naturel"), `tp_levels` (vente échelonnée multi-paliers).
- **Presets** : sauvegarder une config de settings sous un nom (`DataStore.save_preset()`), l'appliquer à un protection target ou un wallet.
- **Filtre "bundle > 15k"** (`analysis/backtest.check_first_candle_filter`) : rejette les tokens dont la 1ère bougie est déjà trop haute avant même le backtest.
- **TP suggéré automatiquement** (`analysis/backtest.suggest_tp_from_history`) : basé sur la médiane des gains observés plutôt que le max, suivant la logique "vendre au milieu, pas au sommet" des vidéos.
- **Score de régularité** (`analysis/wallet_history.check_sell_regularity`) : mesure si les résultats de trade sont cohérents d'un token à l'autre (faible dispersion = wallet prévisible = bon candidat).

### Limites honnêtes de ces nouveaux ajouts

- **`no_activity_sell_s`** utilise `txns.m5` de DexScreener (résolution 5 minutes) comme proxy pour détecter l'inactivité — F Project lit directement la blockchain et peut détecter une inactivité de 35 secondes précisément. Notre version sera moins réactive.
- **`check_first_candle_filter`** utilise le market cap *actuel* du token comme proxy pour "market cap fin de 1ère bougie" — imprécis. Une vraie implémentation capturerait le market cap au moment exact de la détection WebSocket.
- **`check_sell_regularity`** mesure la régularité du *résultat de trade* (TP/SL touché de façon cohérente), pas directement "le dev vend toujours au même multiple de market cap" comme décrit dans les vidéos — approximation raisonnable mais pas équivalente, faute de données OHLC historiques précises et gratuites.
- **`buy_on_dev_sell`** (racheter après une vente partielle du dev) reste un stub non implémenté — mentionné dans les settings mais aucune logique de détection de "vente du dev" n'existe encore dans le pipeline.

## Bot Telegram (structure F Project)

`telegram_bot.py` reproduit la navigation documentée sur https://f-project-1.gitbook.io : menu Ruggers (liste, ajout, config par wallet), Settings (Gas Fees, Notifications), Positions (vente rapide), More (Quick Buy by CA).

### Setup Telegram

```bash
export BOT_TOKEN="ton_token_telegram"
```

Envoie `/start` au bot pour t'enregistrer (le `chat_id` est sauvegardé dans `sniper_data.json`, nécessaire pour recevoir les notifications).

### Fonctionnalités implémentées et réellement câblées

- Navigation complète : Ruggers / Settings / Positions / More, avec pagination sur la liste des ruggers
- Config par rugger : Auto-Buy/Auto-Sell (toggle), Tracking Mode, Buy Config (montant, snipe delay, min/max MC, buy only once, Max Token Age, Follow Cooldown), Sell Config (TP multi-niveaux, SL, no-activity-sell), Protection (mode, fresh wallet, keep address, héritage, transfer ranges)
- **Max Loss Counter** : coupe l'auto-buy après N pertes consécutives sur un wallet, réactivation manuelle
- **Buy The Dip** : jusqu'à 3 paliers de DCA sous l'ATH (market cap), suivi correctement via le modèle `units`/`cost_basis_usd`
- **Sell Initials** : récupère exactement la mise investie, laisse le reste en position à coût nul
- **Trailing SL multi-paliers par market cap** : jusqu'à 5 paliers, seuil trailing différent selon le MC atteint
- **Buy Mode Simple/Hardcore** : simulé (voir limite ci-dessous) avec écran de confirmation avant activation Hardcore
- **Referral** : code généré et affiché (voir limite ci-dessous)
- **Child Address Defaults** : réglages appliqués aux wallets ajoutés via Protection quand l'héritage est désactivé
- Toggles globaux Auto-Buy/Auto-Sell, Presets, Notifications configurables, Quick Buy by CA

### Fonctionnalités "best-effort" — implémentées mais avec des limites réelles à connaître

- **Auto-Sell on Big Buy** : détecte les gros achats en pollant les dernières transactions du pool via Helius (résolution ~15s, le temps du cycle de `_monitor_position`). F Project lit les logs de validateur en direct — nous non. Peut rater des big buys entre deux polls, ou réagir avec quelques secondes de retard.
- **Front Run Sell** : même limite de polling (~15s). Le concept documenté ("vendre dans le même bloc que la consolidation") n'est PAS reproduit — on détecte la consolidation après coup, pas en même-bloc. Le seuil de consolidation est vérifié en sommant les soldes de tokens des wallets connus du même groupe de liaison (créés via Rugger Protection), pas via une vraie analyse de tous les wallets liés au dev.

### Fonctionnalités volontairement simulées/cosmétiques (pas de sens en PAPER trading)

- **Buy Mode Simple/Hardcore** : F Project fait tourner une vraie course multi-serveurs (5 régions, multi-nonce) — impossible à répliquer sans cette infra. On simule juste un taux d'échec de transaction plus élevé en Hardcore (25%, aléatoire) et des frais simulés plus hauts, pour illustrer le compromis documenté, sans aucune vraie course de blocs.
- **Referral** : aucun système de paiement/commission réel — F Project gère de vrais paiements entre comptes payants, ce qui n'existe pas ici (bot personnel, pas de commerce). Le code généré est purement déclaratif.

### Ce qui reste un stub complet (visible mais non exécuté)

- **Buy on Dev Sell** : sélectionnable comme Tracking Mode, mais le pipeline ne détecte pas les ventes du dev pour déclencher un rachat
- **💰 Wallets réels** : absent volontairement, le bot est en PAPER trading
- **Track Sells / Copier le % de Vente** (copy trading) : sélectionnable comme mode mais pas exécuté, le pipeline reste focalisé sur le sniping de dev (`track_creation`)

## Setup

```bash
pip install -r requirements.txt

export HELIUS_API_KEY="ta_clé"
export BOT_TOKEN="ton_token_telegram"      # alertes Telegram et commandes autorisées
export TRADING_MODE="PAPER"                 # ne JAMAIS changer avant validation complète

python main.py
```

## Ce qui fonctionne dès maintenant
- Connexion WebSocket aux logs Pump.fun (si clé Helius valide)
- Traçage du funder d'un wallet + détection schéma "exchange" (si adresses vérifiées)
- Backtest approximatif sur l'historique d'un dev
- Paper trading avec TP/SL simulés; état persistant SQLite par défaut et import initial depuis l'ancien JSON.
- Collecteur wallet optionnel en lecture seule, avec curseurs et stockage SQLite.
- Relecture de chandelles OHLC fournie par l'utilisateur avec frais, slippage et règles conservatrices dans `historical_backtest.py`.
- PnL LIVE basé sur les soldes on-chain confirmés et les frais, sans utiliser le montant estimé du devis.

## Ce qu'il reste à construire
- Régularité de vente du dev (point 4 ci-dessus)
- Distinction fiable simple/mère (point 5)
- Source historique OHLC automatique et données de stratégie validées; le moteur actuel requiert un jeu de chandelles fourni.
- Tests d'intégration RPC/Jupiter et soak PAPER en environnement cible.
- Revue indépendante de la politique on-chain, de la reprise après crash et des limites avant toute activation LIVE.
- L'exécution utilise Jupiter Swap API V2 `/order` + `/execute`, requiert `JUPITER_API_KEY` et reste non validée.

## Prochaine étape recommandée
Lance `python -m detection.websocket_listener` seul, avec ta clé Helius, et observe
ce qui sort en conditions réelles pendant 10-15 minutes. Ça va révéler immédiatement
si le format de réponse Helius correspond à ce que le code attend, avant d'aller plus loin.


## Mise en route

Copiez `.env.example` vers `.env`, gardez `TRADING_MODE=PAPER`, puis suivez [INSTALLATION.md](INSTALLATION.md) et [CONFIGURATION.md](CONFIGURATION.md). Les identifiants Telegram doivent figurer dans `TELEGRAM_ALLOWED_USER_IDS` (identifiants numériques séparés par des virgules). Sans allowlist, commandes et callbacks sont refusés. L'audit et les limites restantes sont dans [AUDIT_COMPLET.md](AUDIT_COMPLET.md).


## Phase fiabilité et sécurité

La branche de suivi ajoute un état SQLite WAL avec sauvegardes/récupération, un arrêt d'urgence persistant (`/halt`, reprise refusée tant qu'une signature reste irrésolue), un collecteur de wallets strictement en lecture seule, des alertes RPC/collecteur, et des plafonds globaux d'exposition/nombre de positions. L'exécuteur Jupiter V2 n'autorise que le routeur Metis, valide le payeur, les programmes top-level, le blockhash, les frais et le loyer estimé avant signature. Toute transaction envoyée est d'abord inscrite durablement; les résultats sont comptabilisés à partir des deltas de soldes confirmés. Une issue incertaine arrête les nouvelles exécutions.

Ces contrôles sont couverts par des tests mockés. Ils ne constituent pas une validation on-chain ni un feu vert LIVE. Gardez `TRADING_MODE=PAPER`.
