"""
════════════════════════════════════════════════════════════════
TELEGRAM BOT — Interface de contrôle (structure F Project)
════════════════════════════════════════════════════════════════
Reproduit la navigation du bot F Project documentée sur
https://f-project-1.gitbook.io :

Menu Principal
├── 🎯 Ruggers (liste, ajout, presets, toggles globaux)
│   └── [clic sur un rugger] → Config du Rugger
│       ├── 🔔 Tracking Mode
│       ├── 💰 Buy Config
│       ├── 📈 Sell Config
│       ├── 🛡️ Protection
│       ├── 🛑 Max Loss Counter
│       ├── 📋 Save Preset / 🔄 Reset / ✏️ Rename / 🗑 Delete
├── ⚙️ Settings (Gas Fees, Notifications)
├── 📊 Positions (vente rapide 25/50/75/100%, Sell All)
└── ••• More (Quick Buy by CA, liens)

Ce que ce module NE couvre PAS (hors scope "sniping de dev", ou stub non exécuté) :
- 💰 Wallets réels (le bot est en PAPER trading, pas de wallet on-chain à gérer)
- Copier le % de Vente / Track Sells (copy trading, hors scope de cette session)
- Front Run Sell (stub — settings exposés mais aucune détection de consolidation
  on-chain n'est implémentée dans le pipeline actuel)
- Multi Buy / plusieurs wallets d'exécution (nécessite du trading LIVE réel)

Utilise python-telegram-bot >= 21.
"""

import asyncio
import logging
import copy
import os

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)

import re
import config
from translations import t

log = logging.getLogger("telegram_bot")

# état conversationnel léger : chat_id -> {"awaiting": "...", "context": {...}}
user_states: dict = {}


def _is_solana_address(text: str) -> bool:
    text = text.strip()
    if not (32 <= len(text) <= 44):
        return False
    allowed = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    return all(c in allowed for c in text)


def _looks_like_token_mint(text: str) -> bool:
    """
    AJOUTÉ suite à un vrai signalement : une adresse de TOKEN collée dans
    un flux attendant une adresse de WALLET (ex: "Analyse de wallet")
    produisait un résultat vide et trompeur ("0 créations, 0 achats") sans
    aucun avertissement — le bot ne faisait aucune différence entre les
    deux types d'adresses. Pump.fun applique systématiquement un suffixe
    vanity "pump" à CHAQUE mint de token qu'il génère (confirmé sur tous
    les exemples réels rencontrés) — un vrai wallet (paire de clés
    normale) n'a quasiment aucune chance de se terminer par ce suffixe
    précis par hasard. Signal fiable, pas parfait à 100%, mais largement
    suffisant pour avertir plutôt que de laisser un résultat vide et
    trompeur sans explication.
    """
    return text.strip().endswith("pump")


def _format_sol_amount(amount: float) -> str:
    """
    AJOUTÉ suite à un vrai cas ambigu observé : un montant de financement
    affiché "0.0000 SOL" pouvait vouloir dire deux choses très différentes
    — un vrai montant minuscule (poussière, arrondi à 4 décimales) ou un
    bug d'extraction — impossible de distinguer les deux à l'affichage.
    Passe à 8 décimales quand la valeur est non-nulle mais arrondirait à
    0.0000 à la précision normale, pour lever l'ambiguïté visuellement.
    """
    if amount != 0 and round(amount, 4) == 0:
        return f"{amount:.8f}"
    return f"{amount:.4f}"


def _compute_star_rating(results: list) -> dict:
    """
    AJOUTÉ suite à une demande explicite : note globale de 1 à 5 étoiles
    pour "Analyse de wallet", combinant tous les trades connus (créations
    ET achats confondus) en un seul score.

    Formule TRANSPARENTE (pas de boîte noire) — combine 3 signaux déjà
    calculés ailleurs dans cette même fonction, chacun ramené sur 100 :
      - win rate (poids 50%) : proportion de trades gagnants
      - ratio gain/perte (poids 30%), plafonné à 5 (au-delà, considéré
        "excellent" sans distinction supplémentaire)
      - résultat moyen par trade (poids 20%), plafonné entre 0% et 100%

    Score combiné 0-100, converti en étoiles par tranches de 20 points
    (0-20 = 1★, 20-40 = 2★, ... 80-100 = 5★).

    Retourne {"stars": int, "score": float, "trade_count": int} ou None
    si moins de 3 trades (échantillon jugé trop petit pour être fiable,
    même seuil que le reste du bot).
    """
    if len(results) < 3:
        return None

    win_rate = len([r for r in results if r["result_pct"] > 0]) / len(results) * 100
    avg_result_pct = sum(r["result_pct"] for r in results) / len(results)

    gains = sum(r["result_pct"] for r in results if r["result_pct"] > 0)
    losses = abs(sum(r["result_pct"] for r in results if r["result_pct"] < 0))
    ratio = (gains / losses) if losses > 0 else 5.0  # aucune perte connue -> ratio plafonné, pas infini

    win_rate_score = min(win_rate, 100)
    ratio_score = min(ratio, 5) / 5 * 100
    avg_result_score = min(max(avg_result_pct, 0), 100)

    combined = win_rate_score * 0.5 + ratio_score * 0.3 + avg_result_score * 0.2
    stars = max(1, min(5, int(combined // 20) + 1))

    return {"stars": stars, "score": combined, "trade_count": len(results)}


async def _compute_bot_probability(trades: list) -> dict:
    """
    AJOUTÉ (demande explicite) : score indicatif "probabilité bot" pour un
    wallet, basé sur deux signaux comportementaux calculables sans appel RPC
    lourd supplémentaire — réutilise wallet_history.get_token_creation_time
    (un seul appel léger par trade, pas le décodage à 150 transactions) :

    1. VITESSE D'ACHAT après création du token — un bot de sniping achète
       systématiquement dans les toutes premières secondes (score élevé si
       la moyenne est très basse).
    2. RÉGULARITÉ de cette vitesse — un humain a un temps de réaction
       variable (curiosité, hésitation, multitâche...), un bot est
       mécaniquement constant d'un trade à l'autre (coefficient de
       variation bas = suspect).

    Ne couvre PAS dans cette version (hors scope, nécessiterait des
    signaux supplémentaires) : la régularité des MONTANTS investis
    (sol_spent n'est pas fiablement disponible partout), ni la détection
    de "wallets en essaim" (plusieurs wallets financés par la même
    source). Purement indicatif — jamais utilisé pour bloquer un ajout
    automatiquement, juste affiché.

    trades : liste de dicts avec au moins "block_time" et "token_mint"
    (même format que wallet_history.get_recent_buys/get_created_tokens).

    Retourne None si moins de 3 trades avec un temps de création
    déterminable (pas assez de données pour un score fiable), sinon
    {"score": int (0-100), "label": str, "fast_buy_pct": float,
    "avg_speed_s": float, "sample_size": int}.
    """
    import wallet_history

    speeds = []
    for trade in trades:
        if not trade.get("block_time") or not trade.get("token_mint"):
            continue
        creation_time = await wallet_history.get_token_creation_time(trade["token_mint"])
        if creation_time is None:
            continue
        speed_s = trade["block_time"] - creation_time
        if speed_s < 0:
            continue  # incohérent (latence RPC/horloge) — ignoré plutôt que de fausser le score
        speeds.append(speed_s)

    if len(speeds) < 3:
        return None

    avg_speed = sum(speeds) / len(speeds)
    fast_pct = sum(1 for s in speeds if s < 30) / len(speeds) * 100

    mean = avg_speed if avg_speed > 0 else 1
    variance = sum((s - avg_speed) ** 2 for s in speeds) / len(speeds)
    coefficient_variation = (variance ** 0.5) / mean

    # Vitesse : ~100 pts pour un achat quasi instantané, ~0 pt au-delà de 5 min.
    speed_score = max(0, min(100, 100 - (avg_speed / 3)))
    # Régularité : coefficient de variation bas (peu de variation) = score haut.
    regularity_score = max(0, 100 - (coefficient_variation * 50))

    score = int(round(speed_score * 0.6 + regularity_score * 0.4))

    if score >= 70:
        label = "🤖 Très probablement un bot"
    elif score >= 40:
        label = "🤔 Comportement mixte / incertain"
    else:
        label = "🙂 Comportement plutôt humain"

    return {"score": score, "label": label, "fast_buy_pct": fast_pct, "avg_speed_s": avg_speed, "sample_size": len(speeds)}


async def _compute_ath_potential(wallet_address: str, max_tokens: int = 12) -> dict:
    """
    AJOUTÉ (demande explicite) : "si j'avais copié les N derniers PREMIERS
    achats de ce wallet et vendu chacun exactement à son market cap
    maximum atteint après l'achat, quel aurait été mon résultat global ?"

    IMPORTANT — ce n'est PAS une simulation réaliste (personne ne vend
    jamais pile au sommet, ce qui rend ce nombre systématiquement optimiste
    par rapport à un vrai résultat de trading) : c'est une mesure du
    POTENTIEL MAXIMAL THÉORIQUE des tokens que ce wallet choisit — utile
    pour juger si ce wallet a l'œil pour repérer des tokens qui PEUVENT
    beaucoup monter, indépendamment du bon moment de sortie. À comparer
    avec le résultat réaliste de "Analyse de wallet" (TP/SL simulés), pas
    à la place.

    Ne garde que le PREMIER achat de chaque token (déduplique — racheter
    plus tard le même token n'est pas un nouveau "pari" distinct).
    Réutilise backtest_token_onchain_pathaware (champ max_gain_pct,
    maintenant exposé) — même méthode de reconstruction on-chain que le
    reste du bot, pas un calcul séparé.

    Retourne None si moins de 3 tokens avec un résultat calculable, sinon
    {"avg_gap_pct": float, "sample_size": int, "details": [{"token_mint",
    "gap_pct"}, ...]}.
    """
    import wallet_history
    import backtest

    # Sur-échantillonne large puis déduplique, pour arriver à max_tokens
    # tokens UNIQUES même si le wallet a parfois racheté le même token
    # plusieurs fois (get_recent_buys peut retourner des doublons de mint).
    raw_buys = await wallet_history.get_recent_buys(wallet_address, max_results=max_tokens * 3)

    seen_mints = set()
    first_buys = []
    for buy in raw_buys:  # déjà trié du plus récent au plus ancien
        mint = buy.get("token_mint")
        if not mint or mint in seen_mints:
            continue
        seen_mints.add(mint)
        first_buys.append(buy)
        if len(first_buys) >= max_tokens:
            break

    if len(first_buys) < 3:
        return None

    details = []
    for buy in first_buys:
        result = await backtest.backtest_token_onchain_pathaware(buy["token_mint"], None, buy.get("block_time"))
        if result is None or "max_gain_pct" not in result:
            continue
        details.append({"token_mint": buy["token_mint"], "gap_pct": result["max_gain_pct"]})

    if len(details) < 3:
        return None

    avg_gap = sum(d["gap_pct"] for d in details) / len(details)
    return {"avg_gap_pct": avg_gap, "sample_size": len(details), "details": details}


class SniperTelegramBot:
    def __init__(self, data_store, notifier, paper_trader):
        self.data_store = data_store
        self.notifier = notifier
        self.trader = paper_trader
        self.app = None

    def build(self):
        self.app = Application.builder().token(config.BOT_TOKEN).build()

        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("stats", self.cmd_stats))
        self.app.add_handler(CommandHandler("ruggers", self.cmd_ruggers))
        self.app.add_handler(CommandHandler("copytrade", self.cmd_copytrade))
        self.app.add_handler(CommandHandler("wallets", self.cmd_wallets))
        self.app.add_handler(CommandHandler("positions", self.cmd_positions))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("settings", self.cmd_settings))
        self.app.add_handler(CommandHandler("analyze", self.cmd_analyze))
        self.app.add_handler(CommandHandler("wallet", self.cmd_wallet_analyze))
        self.app.add_handler(CommandHandler("dev", self.cmd_dev_analyze))
        self.app.add_handler(CallbackQueryHandler(self.on_button))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

        self.notifier.set_bot(self.app.bot)
        return self.app

    async def run_polling(self):
        app = self.build()
        await app.initialize()

        # Enregistre les commandes auprès de Telegram pour qu'elles apparaissent
        # dans le petit menu natif (icône à côté du trombone, dans la barre de
        # saisie) — sans ça, /start et /stats fonctionnent mais restent invisibles
        # dans ce menu, l'utilisateur doit les connaître et les taper à l'aveugle.
        from telegram import BotCommand
        await app.bot.set_my_commands([
            BotCommand("start", "Ouvrir le menu principal"),
            BotCommand("ruggers", "🎯 Ruggeurs (sniping de dev)"),
            BotCommand("copytrade", "📋 Copy Trading"),
            BotCommand("wallets", "💰 Portefeuilles"),
            BotCommand("positions", "📊 Positions ouvertes"),
            BotCommand("analyze", "🔍 Analyser un coin"),
            BotCommand("wallet", "🔎 Analyse de wallet"),
            BotCommand("dev", "🕵️ Analyse de dev"),
            BotCommand("stats", "📊 Statistiques du bot"),
            BotCommand("settings", "⚙️ Settings"),
            BotCommand("help", "ℹ️ Aide"),
        ])

        await app.start()
        await app.updater.start_polling()
        log.info("🤖 Bot Telegram démarré (polling).")

    # ══════════════════════════════════════════════════════════
    # COMMANDES
    # ══════════════════════════════════════════════════════════

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.data_store.set_telegram_chat_id(update.effective_chat.id)

        # MODIFIÉ (demande explicite, 19 août — inspiré d'un screenshot F
        # Project fourni, adapté à FLACH COIN) : /start affiche un véritable
        # écran d'accueil (bannière + présentation des capacités + bouton
        # "Démarrer") plutôt que de sauter directement au menu opérationnel.
        #
        # CORRIGÉ (précisé juste après le premier déploiement) : affiché
        # SEULEMENT à la toute première utilisation — pas à chaque /start —
        # via un flag persisté (state["welcome_screen_shown"], survit à un
        # redémarrage). Les /start suivants vont directement au menu
        # principal, comme avant l'ajout de cet écran.
        if self.data_store.state.get("welcome_screen_shown"):
            await self.show_main_menu(update)
            return

        welcome_text = (
            "⚡ *FLACH COIN*\n_Automated Trading Intelligence_\n\n"
            "🚀 *Ton bot de copy trading Solana*\n\n"
            "🎯 Automatise le sniping et le copy trading\n"
            "💼 Gère ton portefeuille et tes positions\n"
            "🔒 Sécurisé — aucune clé privée exposée dans le chat\n"
            "⚙️ Entièrement personnalisable, wallet par wallet\n"
            "📊 Suit tes performances en temps réel\n"
            "🛡️ Protection proactive (schémas mère/exchange)\n\n"
            "Configure tes préférences, surveille des wallets, et laisse "
            "le bot exécuter les trades selon tes réglages.\n\n"
            "Démarre ton trading automatisé dès maintenant ! 🌟"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Démarrer", callback_data="menu_main")]])

        self.data_store.state["welcome_screen_shown"] = True
        self.data_store.save()

        if config.BANNER_IMAGE_PATH and os.path.isfile(config.BANNER_IMAGE_PATH):
            try:
                with open(config.BANNER_IMAGE_PATH, "rb") as photo:
                    await update.message.reply_photo(
                        photo=photo, caption=welcome_text, parse_mode="Markdown", reply_markup=keyboard,
                    )
                return
            except Exception as e:
                log.warning(f"Impossible d'envoyer la bannière ({e}) — retombe sur le texte seul.")

        await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=keyboard)

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        state = self.data_store.state
        n_ruggers, n_copytrade = self.data_store.count_wallets_by_mode()
        text = (
            f"📊 *Stats*\n\n"
            f"P&L total: {state.get('total_pnl_usd', 0):+.2f}$\n"
            f"Pertes de session: {state.get('session_losses_usd', 0):.2f}$\n"
            f"Positions ouvertes: {len(state.get('open_positions', []))}\n"
            f"Positions clôturées: {len(state.get('closed_positions', []))}\n"
            f"Ruggers monitorés: {n_ruggers}\n"
            f"Copy Trading: {n_copytrade}\n"
            f"Total: {self.data_store.count_wallets()}/{config.MAX_MONITORED_WALLETS}"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    # ── Nouvelles commandes slash — raccourcis directs vers les menus,
    # pour que le bouton "Menu" natif de Telegram donne accès à tout,
    # pas seulement /start et /stats (demande explicite). Réutilisent les
    # mêmes fonctions que les boutons — _send_or_edit gère déjà les deux
    # cas (query de bouton, ou update de commande slash) sans modification.
    async def cmd_ruggers(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.show_ruggers_menu(update)

    async def cmd_copytrade(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.show_copytrade_menu(update)

    async def cmd_wallets(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.show_wallets_menu(update)

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.show_positions(update)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.show_help(update)

    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.show_settings_menu(update)

    async def cmd_analyze(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Raccourci slash pour 🔍 Analyser un coin — supporte aussi /analyze <adresse> directement."""
        chat_id = update.effective_chat.id
        if context.args:
            token_address = context.args[0]
            if _is_solana_address(token_address):
                await self.analyze_coin_inline(update, token_address)
                return
        user_states[chat_id] = {"awaiting": "analyze_coin_address"}
        await update.message.reply_text("Colle l'adresse du token à analyser.")

    async def cmd_wallet_analyze(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Raccourci slash pour 🔎 Analyse de wallet — supporte aussi /wallet <adresse> directement."""
        chat_id = update.effective_chat.id
        if context.args:
            wallet_address = context.args[0]
            if _is_solana_address(wallet_address):
                await self.analyze_wallet_inline(update, wallet_address)
                return
        user_states[chat_id] = {"awaiting": "analyze_wallet_address"}
        await update.message.reply_text("Colle l'adresse du WALLET à analyser (financement, statut dev, statut trader).")

    async def cmd_dev_analyze(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Raccourci slash pour 🕵️ Analyse de dev — supporte aussi /dev <adresse> directement."""
        chat_id = update.effective_chat.id
        if context.args:
            dev_address = context.args[0]
            if _is_solana_address(dev_address):
                await self.analyze_dev_inline(update, dev_address)
                return
        user_states[chat_id] = {"awaiting": "analyze_dev_address"}
        await update.message.reply_text("Colle l'adresse du DEV à analyser (jusqu'à 12 dernières créations + statut fresh wallet).")

    # ══════════════════════════════════════════════════════════
    # MENU PRINCIPAL
    # ══════════════════════════════════════════════════════════

    def _build_main_menu_content(self):
        """Construit (texte, clavier) du menu principal — réutilisé par cmd_start
        (avec bannière photo) et show_main_menu (retour texte simple)."""
        state = self.data_store.state
        lang = state.get("language", "fr")
        auto_buy = "🟢 ON" if state.get("global_auto_buy", True) else "🔴 OFF"
        auto_sell = "🟢 ON" if state.get("global_auto_sell", True) else "🔴 OFF"

        n_ruggers, n_copytrade = self.data_store.count_wallets_by_mode()
        text = (
            f"*⚡ FLACH COIN*\n\n"
            f"{t('main_menu_mode', lang)} : `{config.TRADING_MODE}`\n"
            f"{t('main_menu_autobuy', lang)}: {auto_buy}\n"
            f"{t('main_menu_autosell', lang)}: {auto_sell}\n"
            f"{t('main_menu_ruggers', lang)}: {n_ruggers} | Copy Trading: {n_copytrade} (total {self.data_store.count_wallets()}/{config.MAX_MONITORED_WALLETS})\n"
            f"P&L: {state.get('total_pnl_usd', 0):+.2f}$"
        )

        lang_button_key = "language_button_fr" if lang == "fr" else "language_button_en"

        keyboard = [
            [
                InlineKeyboardButton(t("btn_ruggers", lang), callback_data="menu_ruggers"),
                InlineKeyboardButton(t("btn_copytrade", lang), callback_data="menu_copytrade"),
            ],
            [
                InlineKeyboardButton(t("btn_wallets", lang), callback_data="menu_wallets"),
                InlineKeyboardButton(t("btn_positions", lang), callback_data="menu_positions"),
            ],
            [
                InlineKeyboardButton(t("btn_analyzecoin", lang), callback_data="analyzecoin"),
                InlineKeyboardButton(t("btn_analyzewallet", lang), callback_data="analyzewallet"),
            ],
            [
                InlineKeyboardButton(t("btn_analyzedev", lang), callback_data="analyzedev"),
            ],
            [
                InlineKeyboardButton(t("btn_security", lang), callback_data="checksecurity"),
                InlineKeyboardButton(t("btn_aiscore", lang), callback_data="checkaiscore"),
            ],
            [
                InlineKeyboardButton(t("btn_poscalc", lang), callback_data="menu_poscalc"),
                InlineKeyboardButton(t("btn_strategies", lang), callback_data="menu_strategies"),
            ],
            [
                InlineKeyboardButton(t("btn_journal", lang), callback_data="menu_journal"),
                InlineKeyboardButton(t("btn_sessionstats", lang), callback_data="menu_sessionstats"),
            ],
            [
                InlineKeyboardButton(t("btn_toggleai", lang), callback_data="toggleglobalai"),
                InlineKeyboardButton(t("btn_alerts", lang), callback_data="menu_alertstoggle"),
            ],
            [
                InlineKeyboardButton(t("btn_stop", lang), callback_data="menu_globalstop"),
                InlineKeyboardButton(t(lang_button_key, lang), callback_data="togglelanguage"),
            ],
            [
                InlineKeyboardButton(t("btn_more", lang), callback_data="menu_more"),
                InlineKeyboardButton(t("btn_help", lang), callback_data="menu_help"),
            ],
        ]
        return text, InlineKeyboardMarkup(keyboard)

    async def show_main_menu(self, update_or_query, edit: bool = False):
        text, markup = self._build_main_menu_content()
        await self._send_or_edit(update_or_query, text, markup, edit)

    # ══════════════════════════════════════════════════════════
    # 🎯 RUGGERS
    # ══════════════════════════════════════════════════════════

    async def show_ruggers_menu(self, query, page: int = 0):
        state = self.data_store.state
        lang = state.get("language", "fr")
        # Ne montre que les wallets en mode sniping de dev — les wallets copytrade
        # (track_buy/track_sell) vivent désormais dans leur propre menu 📋 Copy Trading.
        # AJOUTÉ (demande explicite, 19 août) : exclut aussi les adresses de
        # retrait rattachées en arrière-plan à un parent (linked_to_parent) —
        # toujours surveillées normalement, juste plus affichées comme ligne
        # séparée ici. Voir monitoring_list.add_dev_wallet.
        wallets = {a: e for a, e in self.data_store.list_wallets().items()
                   if e.get("mode", "track_creation") in ("track_creation", "buy_on_dev_sell")
                   and not e.get("linked_to_parent")}

        auto_buy = "🟢 ON" if state.get("global_auto_buy", True) else "🔴 OFF"
        auto_sell = "🟢 ON" if state.get("global_auto_sell", True) else "🔴 OFF"

        text = t("ruggers_menu_title", lang, count=len(wallets), max=config.MAX_MONITORED_WALLETS)

        keyboard = [
            [
                InlineKeyboardButton(f"{t('btn_autobuy', lang)}: {auto_buy}", callback_data="toggle_global_auto_buy"),
                InlineKeyboardButton(f"{t('btn_autosell', lang)}: {auto_sell}", callback_data="toggle_global_auto_sell"),
            ],
            [
                InlineKeyboardButton(t("btn_add_rugger", lang), callback_data="add_rugger"),
                InlineKeyboardButton(t("btn_presets", lang), callback_data="list_presets"),
            ],
        ]

        # Pagination simple, 5 par page
        items = list(wallets.items())
        page_size = 5
        start = page * page_size
        page_items = items[start:start + page_size]

        for address, entry in page_items:
            label = entry.get("label", address[:8] + "...")
            status = "⏸ PAUSED" if entry["settings"].get("paused") else "🟢"
            # AJOUTÉ (demande explicite) : compteur d'adresses de retrait
            # rattachées en arrière-plan à ce wallet, à la place des lignes
            # séparées qu'elles occupaient avant.
            linked_count = self.data_store.count_linked_wallets(address)
            linked_suffix = f" ({linked_count} adresse{'s' if linked_count > 1 else ''} liée{'s' if linked_count > 1 else ''})" if linked_count else ""
            keyboard.append([InlineKeyboardButton(
                f"{status} {label} ({entry.get('mode', 'track_creation')}){linked_suffix}",
                callback_data=f"rugger_{self._sid(address)}",
            )])

        nav_row = []
        if start > 0:
            nav_row.append(InlineKeyboardButton("◀️", callback_data=f"ruggers_page_{page-1}"))
        if start + page_size < len(items):
            nav_row.append(InlineKeyboardButton("▶️", callback_data=f"ruggers_page_{page+1}"))
        if nav_row:
            keyboard.append(nav_row)

        keyboard.append([InlineKeyboardButton(t("btn_back", lang), callback_data="menu_main")])
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_copytrade_menu(self, query, page: int = 0):
        """
        Menu séparé pour les wallets copy trading (mode track_buy/track_sell),
        distinct du menu Ruggeurs (sniping de dev). Réutilise la même config
        de wallet (settings partagés) mais liste/ajoute séparément.
        """
        lang = self.data_store.state.get("language", "fr")
        # AJOUTÉ (demande explicite, 19 août) : filtre défensif — les wallets
        # Copy Trading n'obtiennent plus JAMAIS d'adresse de retrait rattachée
        # (voir main.on_wallet_withdrawal, exclusion explicite), mais ce
        # filtre protège quand même contre une éventuelle entrée héritée.
        wallets = {a: e for a, e in self.data_store.list_wallets().items()
                   if e.get("mode") in ("track_buy", "track_sell") and not e.get("linked_to_parent")}

        text = (
            f"{t('copytrade_menu_title', lang, count=len(wallets))}\n\n"
            f"{t('copytrade_menu_subtitle', lang)}"
        )

        keyboard = [
            [InlineKeyboardButton(t("btn_add_copytrade", lang), callback_data="add_copytrade")],
        ]

        items = list(wallets.items())
        page_size = 5
        start = page * page_size
        page_items = items[start:start + page_size]

        for address, entry in page_items:
            label = entry.get("label", address[:8] + "...")
            mode_display = t("mode_buy_display", lang) if entry.get("mode") == "track_buy" else t("mode_sell_display", lang)
            status = "⏸ PAUSED" if entry["settings"].get("paused") else "🟢"
            keyboard.append([InlineKeyboardButton(
                f"{status} {label} ({mode_display})",
                callback_data=f"rugger_{self._sid(address)}",
            )])

        nav_row = []
        if start > 0:
            nav_row.append(InlineKeyboardButton("◀️", callback_data=f"copytrade_page_{page-1}"))
        if start + page_size < len(items):
            nav_row.append(InlineKeyboardButton("▶️", callback_data=f"copytrade_page_{page+1}"))
        if nav_row:
            keyboard.append(nav_row)

        keyboard.append([InlineKeyboardButton(t("btn_back", lang), callback_data="menu_main")])
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_rugger_config(self, query, address: str):
        lang = self.data_store.state.get("language", "fr")
        entry = self.data_store.state["monitored_dev_wallets"].get(address)
        if not entry:
            await query.edit_message_text(t("rugger_not_found", lang))
            return

        s = entry["settings"]
        label = entry.get("label", address[:8] + "...")
        tp_summary = ", ".join(f"+{lvl['pct']:.0f}%→{lvl['sell_ratio']*100:.0f}%" for lvl in s.get("tp_levels", []))
        buy_mode = s.get("buy_mode", "simple")
        buy_mode_display = t("buy_mode_hardcore", lang) if buy_mode == "hardcore" else t("buy_mode_simple", lang)

        text = (
            f"⚙️ *{label}*\n`{address}`\n\n"
            f"Buy: {s.get('buy_amount_sol', 0.1)} SOL | Mode: `{entry.get('mode')}`\n"
            f"Buy Mode: {buy_mode_display}\n"
            f"TP: {tp_summary} | SL: {s.get('sl_pct', 'off')}%\n"
            f"{t('status_label', lang)}: {t('status_paused', lang) if s.get('paused') else t('status_active', lang)}"
        )

        ab = "🟢 ON" if s.get("auto_buy") else "🔴 OFF"
        as_ = "🟢 ON" if s.get("auto_sell") else "🔴 OFF"

        # MODIFIÉ (demande explicite, 19 août) : cet écran avait grossi à 14
        # boutons au fil des sessions — max 5 boutons "importants" visibles
        # directement maintenant, le reste déplacé dans "••• Plus" (voir
        # show_rugger_config_more). Choix des 5 faits par défaut (à ajuster
        # si besoin) : Auto-Buy, Auto-Sell, Analyse contextuelle (nouveau),
        # Buy Config, Sell Config — les réglages les plus consultés au
        # quotidien, contrairement à Snipe Config/Max Loss Counter/
        # Rename/Reset/Delete, plus rares une fois le wallet configuré.
        #
        # AJOUTÉ (demande explicite) : bouton d'analyse contextuel — plus
        # besoin de retourner au menu principal et recoller l'adresse.
        # "Analyse de dev" pour un Ruggeur (track_creation/buy_on_dev_sell),
        # "Analyse de wallet" pour un Copy Trading (track_buy/track_sell) —
        # le bon profil selon ce que ce wallet est réellement suivi pour.
        is_dev_profile = entry.get("mode") in ("track_creation", "buy_on_dev_sell")
        analyze_btn = (
            InlineKeyboardButton(t("btn_analyze_this_dev", lang), callback_data=f"analyzethisdev_{self._sid(address)}")
            if is_dev_profile else
            InlineKeyboardButton(t("btn_analyze_this_wallet", lang), callback_data=f"analyzethiswallet_{self._sid(address)}")
        )

        keyboard = [
            [
                InlineKeyboardButton(f"{t('btn_autobuy', lang)}: {ab}", callback_data=f"toggle_ab_{self._sid(address)}"),
                InlineKeyboardButton(f"{t('btn_autosell', lang)}: {as_}", callback_data=f"toggle_as_{self._sid(address)}"),
            ],
            [analyze_btn],
            [InlineKeyboardButton(t("btn_buy_config", lang), callback_data=f"buyconfig_{self._sid(address)}")],
            [InlineKeyboardButton(t("btn_sell_config", lang), callback_data=f"sellconfig_{self._sid(address)}")],
            [InlineKeyboardButton(t("btn_more", lang), callback_data=f"ruggermore_{self._sid(address)}")],
            [InlineKeyboardButton(
                t("btn_back", lang),
                callback_data="menu_copytrade" if entry.get("mode") in ("track_buy", "track_sell") else "menu_ruggers",
            )],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_rugger_config_more(self, query, address: str):
        """
        ••• Plus — AJOUTÉ (demande explicite, 19 août) : les boutons retirés
        de l'écran principal show_rugger_config pour rester à 5 maximum.
        Rien de fonctionnel n'a changé ici, juste déplacé.
        """
        lang = self.data_store.state.get("language", "fr")
        entry = self.data_store.state["monitored_dev_wallets"].get(address)
        if not entry:
            await query.edit_message_text(t("rugger_not_found", lang))
            return

        s = entry["settings"]
        buy_mode = s.get("buy_mode", "simple")
        buy_mode_display = t("buy_mode_hardcore", lang) if buy_mode == "hardcore" else t("buy_mode_simple", lang)

        keyboard = [
            [InlineKeyboardButton(f"{t('btn_buymode_prefix', lang)}: {buy_mode_display}", callback_data=f"buymode_{self._sid(address)}")],
            [InlineKeyboardButton(t("btn_tracking_mode", lang), callback_data=f"tracking_{self._sid(address)}")],
            [InlineKeyboardButton(t("btn_protection", lang), callback_data=f"protection_{self._sid(address)}")],
            [InlineKeyboardButton(t("btn_security_ai", lang), callback_data=f"securityai_{self._sid(address)}")],
            [InlineKeyboardButton(t("btn_snipe_config", lang), callback_data=f"snipeconfig_{self._sid(address)}")],
            [InlineKeyboardButton(t("btn_maxloss", lang), callback_data=f"maxloss_{self._sid(address)}")],
            [InlineKeyboardButton(t("btn_savepreset", lang), callback_data=f"savepreset_{self._sid(address)}")],
            [
                InlineKeyboardButton(t("btn_rename", lang), callback_data=f"rename_{self._sid(address)}"),
                InlineKeyboardButton(t("btn_reset", lang), callback_data=f"askreset_{self._sid(address)}"),
            ],
            [InlineKeyboardButton(t("btn_delete", lang), callback_data=f"askdelete_{self._sid(address)}")],
            [InlineKeyboardButton(t("btn_back", lang), callback_data=f"rugger_{self._sid(address)}")],
        ]
        await self._send_or_edit(query, f"⚙️ *{entry.get('label', address[:8] + '...')}* — ••• Plus", InlineKeyboardMarkup(keyboard), edit=True)

    async def show_buy_mode(self, query, address: str):
        lang = self.data_store.state.get("language", "fr")
        s = self.data_store.get_wallet_settings(address)
        current = s.get("buy_mode", "simple")
        text = t("buy_mode_title", lang)
        keyboard = []
        if current == "hardcore":
            keyboard.append([InlineKeyboardButton(t("btn_hardcore_active_switch_simple", lang), callback_data=f"setbuymode_{self._sid(address)}_simple")])
        else:
            keyboard.append([InlineKeyboardButton(t("btn_switch_hardcore", lang), callback_data=f"confirmhardcore_{self._sid(address)}")])
        keyboard.append([InlineKeyboardButton(t("btn_back", lang), callback_data=f"rugger_{self._sid(address)}")])
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_hardcore_confirmation(self, query, address: str):
        lang = self.data_store.state.get("language", "fr")
        text = t("hardcore_confirm_title", lang)
        keyboard = [
            [InlineKeyboardButton(t("btn_confirm_hardcore", lang), callback_data=f"setbuymode_{self._sid(address)}_hardcore")],
            [InlineKeyboardButton(t("btn_back_to_simple", lang), callback_data=f"setbuymode_{self._sid(address)}_simple")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_tracking_mode(self, query, address: str):
        lang = self.data_store.state.get("language", "fr")
        entry = self.data_store.state["monitored_dev_wallets"][address]
        current = entry.get("mode", "track_creation")

        def mark(mode):
            return "✅ " if current == mode else ""

        text = t("tracking_mode_title", lang)
        keyboard = [
            [InlineKeyboardButton(f"{mark('track_creation')}{t('btn_track_creation', lang)}", callback_data=f"setmode_{self._sid(address)}_track_creation")],
            [InlineKeyboardButton(f"{mark('track_buy')}{t('btn_track_buy', lang)}", callback_data=f"setmode_{self._sid(address)}_track_buy")],
            [InlineKeyboardButton(f"{mark('track_sell')}{t('btn_track_sell', lang)}", callback_data=f"setmode_{self._sid(address)}_track_sell")],
            [InlineKeyboardButton(f"{mark('buy_on_dev_sell')}{t('btn_buy_on_dev_sell', lang)}", callback_data=f"setmode_{self._sid(address)}_buy_on_dev_sell")],
            [InlineKeyboardButton(t("btn_back", lang), callback_data=f"rugger_{self._sid(address)}")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_buy_config(self, query, address: str):
        lang = self.data_store.state.get("language", "fr")
        s = self.data_store.get_wallet_settings(address)
        dip_summary = "\n".join(
            t("dip_level_line", lang, n=i + 1, drop=lvl["drop_pct"], amount=lvl["amount_sol"])
            for i, lvl in enumerate(s.get("buy_the_dip_levels", []))
        ) or f"  {t('no_dip_level_configured', lang)}"
        off = t("off_value", lang)
        text = (
            f"{t('buy_config_title', lang)}\n\n"
            f"Buy Amount: `{s.get('buy_amount_sol', 0.1)}` SOL\n"
            f"Snipe Delay: `{s.get('snipe_delay_s', 0)}` s\n"
            f"Min MC: `{s.get('min_market_cap') or off}`\n"
            f"Max MC: `{s.get('max_market_cap') or off}`\n"
            f"Buy Only Once: `{s.get('buy_only_once')}`\n"
            f"Max Token Age (copytrade): `{s.get('max_token_age_min') or off}` min\n"
            f"Follow Cooldown (copytrade): `{s.get('follow_cooldown_s') or off}` s\n\n"
            f"{t('buy_the_dip_label', lang)} :\n{dip_summary}"
        )
        keyboard = [
            [InlineKeyboardButton(t("btn_buy_amount", lang), callback_data=f"editset_{self._sid(address)}|buy_amount_sol|float")],
            [InlineKeyboardButton(t("btn_snipe_delay", lang), callback_data=f"editset_{self._sid(address)}|snipe_delay_s|float")],
            [InlineKeyboardButton(t("btn_min_mc", lang), callback_data=f"editset_{self._sid(address)}|min_market_cap|float|none")],
            [InlineKeyboardButton(t("btn_max_mc", lang), callback_data=f"editset_{self._sid(address)}|max_market_cap|float|none")],
            [InlineKeyboardButton(
                f"{t('btn_buy_only_once', lang)}: {'🟢' if s.get('buy_only_once') else '🔴'}",
                callback_data=f"toggleset_{self._sid(address)}_buy_only_once",
            )],
            [InlineKeyboardButton(t("btn_max_token_age", lang), callback_data=f"editset_{self._sid(address)}|max_token_age_min|float")],
            [InlineKeyboardButton(t("btn_follow_cooldown", lang), callback_data=f"editset_{self._sid(address)}|follow_cooldown_s|float")],
            [InlineKeyboardButton(t("btn_buy_the_dip", lang), callback_data=f"buythedip_{self._sid(address)}")],
            [InlineKeyboardButton(t("btn_multibuy", lang), callback_data=f"multibuy_{self._sid(address)}")],
            [InlineKeyboardButton(t("btn_back", lang), callback_data=f"rugger_{self._sid(address)}")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_buy_the_dip(self, query, address: str):
        s = self.data_store.get_wallet_settings(address)
        levels = s.get("buy_the_dip_levels", [])
        summary = "\n".join(
            f"Niveau {i+1}: -{lvl['drop_pct']:.0f}% ATH → +{lvl['amount_sol']} SOL"
            for i, lvl in enumerate(levels)
        ) or "Aucun palier configuré (max 3)."
        text = f"📉 *Buy The Dip*\n\n{summary}\n\nEnvoie jusqu'à 3 paliers en une fois."
        keyboard = [
            [InlineKeyboardButton("✏️ Définir les paliers", callback_data=f"editdip_{self._sid(address)}")],
            [InlineKeyboardButton("🗑 Vider les paliers", callback_data=f"cleardip_{self._sid(address)}")],
            [InlineKeyboardButton("← Back", callback_data=f"buyconfig_{self._sid(address)}")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_multibuy_config(self, query, address: str):
        """
        MultiBuy : répartit l'achat sur plusieurs wallets gérés (wallet_manager.py)
        pour réduire le price impact/la détectabilité (voir README pour la méthode
        décrite dans les vidéos). Nécessite d'avoir déjà créé/importé des wallets
        via 💰 Portefeuilles.
        """
        s = self.data_store.get_wallet_settings(address)
        labels = s.get("multibuy_wallet_labels", [])
        labels_display = ", ".join(labels) if labels else "aucun"

        text = (
            f"🧩 *MultiBuy*\n\n"
            f"_Répartit l'achat total sur plusieurs wallets gérés, avec un délai "
            f"entre chaque, pour réduire le risque de détection._\n\n"
            f"Statut : `{'🟢 activé' if s.get('multibuy_enabled') else '🔴 désactivé'}`\n"
            f"Wallets utilisés : `{labels_display}`\n"
            f"Délai entre sous-achats : `{s.get('multibuy_delay_s', 3)}`s\n\n"
            f"⚠️ Nécessite des wallets créés/importés via 💰 Portefeuilles au préalable."
        )
        keyboard = [
            [InlineKeyboardButton(
                f"MultiBuy: {'🟢 ON' if s.get('multibuy_enabled') else '🔴 OFF'}",
                callback_data=f"toggleset_{self._sid(address)}_multibuy_enabled",
            )],
            [InlineKeyboardButton("✏️ Wallets à utiliser", callback_data=f"editmultibuywallets_{self._sid(address)}")],
            [InlineKeyboardButton("✏️ Délai (s)", callback_data=f"editset_{self._sid(address)}|multibuy_delay_s|float")],
            [InlineKeyboardButton("← Back", callback_data=f"buyconfig_{self._sid(address)}")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_sell_config(self, query, address: str):
        lang = self.data_store.state.get("language", "fr")
        s = self.data_store.get_wallet_settings(address)
        tp_summary = "\n".join(t("tp_summary_line", lang, n=i + 1, pct=lvl["pct"], ratio=lvl["sell_ratio"] * 100)
                                for i, lvl in enumerate(s.get("tp_levels", [])))
        big_buy_summary = "\n".join(
            t("bigbuy_level_line", lang, n=i + 1, min=lvl["min_sol"], max=lvl["max_sol"], ratio=lvl["sell_ratio"] * 100)
            for i, lvl in enumerate(s.get("auto_sell_big_buy_levels", []))
        ) or f"  {t('no_dip_level_configured', lang)}"
        off = t("off_value", lang)
        text = (
            f"{t('sell_config_title', lang)}\n\n"
            f"{tp_summary}\n"
            f"SL: `{s.get('sl_pct', off)}`%\n"
            f"No Activity Sell: `{s.get('no_activity_sell_s') or off}` s\n"
            f"Trailing SL: `{s.get('trailing_sl_enabled')}`\n\n"
            f"{t('auto_sell_bigbuy_label', lang)} :\n{big_buy_summary}"
        )
        keyboard = [
            [InlineKeyboardButton(t("btn_edit_tp", lang), callback_data=f"edittp_{self._sid(address)}")],
            [InlineKeyboardButton(t("btn_sl_pct", lang), callback_data=f"editset_{self._sid(address)}|sl_pct|float|none")],
            [InlineKeyboardButton(t("btn_no_activity", lang), callback_data=f"editset_{self._sid(address)}|no_activity_sell_s|float|none")],
            [InlineKeyboardButton(
                f"{t('btn_trailing_sl', lang)}: {'🟢' if s.get('trailing_sl_enabled') else '🔴'}",
                callback_data=f"toggleset_{self._sid(address)}_trailing_sl_enabled",
            )],
            [InlineKeyboardButton(t("btn_trailing_sl_tiers", lang), callback_data=f"trailingtiers_{self._sid(address)}")],
            [InlineKeyboardButton(t("btn_bigbuy", lang), callback_data=f"bigbuy_{self._sid(address)}")],
            [InlineKeyboardButton(t("btn_advanced_strategies", lang), callback_data=f"advstrategies_{self._sid(address)}")],
            [InlineKeyboardButton(t("btn_back", lang), callback_data=f"rugger_{self._sid(address)}")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_advanced_strategies(self, query, address: str):
        """
        Regroupe les 3 stratégies personnalisées : Achat sur Pullback,
        MC Trailing Sell, et Profit Trail (breakeven puis serré).
        """
        lang = self.data_store.state.get("language", "fr")
        s = self.data_store.get_wallet_settings(address)
        text = (
            f"{t('advanced_strategies_title', lang)}\n\n"
            f"{t('pullback_entry_label', lang)}\n"
            f"{t('status_label', lang)}: `{'🟢' if s.get('pullback_entry_enabled') else '🔴'}`\n"
            f"Max MC: `{s.get('pullback_max_mc')}`$ | Timeout: `{s.get('pullback_timeout_s')}`s\n\n"
            f"{t('mc_trailing_label', lang)}\n"
            f"{t('status_label', lang)}: `{'🟢' if s.get('mc_trailing_enabled') else '🔴'}`\n"
            f"{t('arm_threshold_label', lang)}: `{s.get('mc_trailing_arm_threshold')}`$ → {t('sell_threshold_label', lang)}: `{s.get('mc_trailing_sell_threshold')}`$\n\n"
            f"{t('profit_trail_label', lang)}\n"
            f"{t('status_label', lang)}: `{'🟢' if s.get('profit_trail_enabled') else '🔴'}`\n"
            f"{t('arm_threshold_label', lang)}: +{s.get('profit_trail_arm_pct')}% → {t('floor_label', lang)} +{s.get('profit_trail_initial_floor_pct')}%\n"
            f"{t('tight_mode_from', lang)}: +{s.get('profit_trail_tight_arm_pct')}% ({t('gap_label', lang)} {s.get('profit_trail_gap_pct')}%)"
        )
        keyboard = [
            [InlineKeyboardButton(
                f"1. Pullback: {'🟢' if s.get('pullback_entry_enabled') else '🔴'}",
                callback_data=f"toggleset_{self._sid(address)}_pullback_entry_enabled",
            )],
            [InlineKeyboardButton(t("btn_pullback_max_mc", lang), callback_data=f"editset_{self._sid(address)}|pullback_max_mc|float")],
            [InlineKeyboardButton(t("btn_pullback_timeout", lang), callback_data=f"editset_{self._sid(address)}|pullback_timeout_s|float")],
            [InlineKeyboardButton(
                f"2. MC Trailing: {'🟢' if s.get('mc_trailing_enabled') else '🔴'}",
                callback_data=f"toggleset_{self._sid(address)}_mc_trailing_enabled",
            )],
            [InlineKeyboardButton(t("btn_mc_arm", lang), callback_data=f"editset_{self._sid(address)}|mc_trailing_arm_threshold|float")],
            [InlineKeyboardButton(t("btn_mc_sell", lang), callback_data=f"editset_{self._sid(address)}|mc_trailing_sell_threshold|float")],
            [InlineKeyboardButton(
                f"3. Profit Trail: {'🟢' if s.get('profit_trail_enabled') else '🔴'}",
                callback_data=f"toggleset_{self._sid(address)}_profit_trail_enabled",
            )],
            [InlineKeyboardButton(t("btn_arm_pct", lang), callback_data=f"editset_{self._sid(address)}|profit_trail_arm_pct|float")],
            [InlineKeyboardButton(t("btn_initial_floor", lang), callback_data=f"editset_{self._sid(address)}|profit_trail_initial_floor_pct|float")],
            [InlineKeyboardButton(t("btn_tight_arm", lang), callback_data=f"editset_{self._sid(address)}|profit_trail_tight_arm_pct|float")],
            [InlineKeyboardButton(t("btn_tight_gap", lang), callback_data=f"editset_{self._sid(address)}|profit_trail_gap_pct|float")],
            [InlineKeyboardButton(t("btn_back", lang), callback_data=f"sellconfig_{self._sid(address)}")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_big_buy_config(self, query, address: str):
        s = self.data_store.get_wallet_settings(address)
        levels = s.get("auto_sell_big_buy_levels", [])
        summary = "\n".join(
            f"L{i+1}: {lvl['min_sol']}-{lvl['max_sol']} SOL → vendre {lvl['sell_ratio']*100:.0f}%"
            for i, lvl in enumerate(levels)
        ) or "Aucun palier configuré (max 3)."
        text = (
            f"🐳 *Auto-Sell on Big Buy*\n\n{summary}\n\n"
            f"_⚠️ Best-effort : détection par polling (~15s), pas un flux temps réel "
            f"comme F Project. Voir README pour le détail._"
        )
        keyboard = [
            [InlineKeyboardButton("✏️ Définir les paliers", callback_data=f"editbigbuy_{self._sid(address)}")],
            [InlineKeyboardButton("🗑 Vider les paliers", callback_data=f"clearbigbuy_{self._sid(address)}")],
            [InlineKeyboardButton("← Back", callback_data=f"sellconfig_{self._sid(address)}")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_trailing_tiers(self, query, address: str):
        s = self.data_store.get_wallet_settings(address)
        tiers = s.get("trailing_sl_tiers", [])
        summary = "\n".join(
            f"MC ≥ {t['mc_threshold']:.0f}$ → trailing -{t['trailing_pct']:.0f}%"
            for t in tiers
        ) or "Aucun palier (défaut -20% appliqué partout)"
        text = f"📊 *Trailing SL — Paliers par Market Cap*\n\n{summary}\n\nEnvoie jusqu'à 5 paliers en une fois."
        keyboard = [
            [InlineKeyboardButton("✏️ Définir les paliers", callback_data=f"edittrailing_{self._sid(address)}")],
            [InlineKeyboardButton("← Back", callback_data=f"sellconfig_{self._sid(address)}")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_protection_config(self, query, address: str):
        lang = self.data_store.state.get("language", "fr")
        s = self.data_store.get_wallet_settings(address)
        text = (
            f"{t('protection_title', lang)}\n\n"
            f"Mode: `{s.get('protection_mode')}`\n"
            f"Fresh Wallet: `{s.get('fresh_wallet_only')}`\n"
            f"Keep Address: `{s.get('keep_address')}`\n"
            f"{t('inherit_label', lang)}: `{s.get('inherit_settings')}`\n"
            f"Front Run Sell: `{s.get('front_run_sell_enabled')}` (seuil {s.get('front_run_sell_threshold_pct')}%) "
            f"{t('front_run_sell_beta', lang)}"
        )
        keyboard = [
            [InlineKeyboardButton(
                f"{t('btn_protection_toggle', lang)}: {'🟢 ON' if s.get('protection_enabled') else '🔴 OFF'}",
                callback_data=f"toggleset_{self._sid(address)}_protection_enabled",
            )],
            [InlineKeyboardButton(t("btn_mode", lang), callback_data=f"protmode_{self._sid(address)}")],
            [InlineKeyboardButton(
                f"{t('btn_fresh_wallet', lang)}: {'🟢' if s.get('fresh_wallet_only') else '🔴'}",
                callback_data=f"toggleset_{self._sid(address)}_fresh_wallet_only",
            )],
            [InlineKeyboardButton(
                f"{t('btn_keep_address', lang)}: {'🟢' if s.get('keep_address') else '🔴'}",
                callback_data=f"toggleset_{self._sid(address)}_keep_address",
            )],
            [InlineKeyboardButton(
                f"{t('btn_inherit', lang)}: {'🟢' if s.get('inherit_settings') else '🔴'}",
                callback_data=f"toggleset_{self._sid(address)}_inherit_settings",
            )],
            [InlineKeyboardButton(t("btn_transfer_ranges", lang), callback_data=f"transferranges_{self._sid(address)}")],
            [InlineKeyboardButton(
                f"{t('btn_front_run_sell', lang)}: {'🟢' if s.get('front_run_sell_enabled') else '🔴'}",
                callback_data=f"toggleset_{self._sid(address)}_front_run_sell_enabled",
            )],
            [InlineKeyboardButton(t("btn_front_run_threshold", lang), callback_data=f"editset_{self._sid(address)}|front_run_sell_threshold_pct|float")],
            [InlineKeyboardButton(t("btn_child_defaults", lang), callback_data=f"childdefaults_{self._sid(address)}")],
            [InlineKeyboardButton(t("btn_back", lang), callback_data=f"rugger_{self._sid(address)}")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_child_defaults(self, query, address: str):
        lang = self.data_store.state.get("language", "fr")
        s = self.data_store.get_wallet_settings(address)
        cd = s.get("child_defaults", {})
        text = (
            f"{t('child_defaults_title', lang)}\n\n"
            f"{t('child_defaults_subtitle', lang)}\n\n"
            f"Keep Address: `{cd.get('keep_address')}`\n"
            f"Protection ON: `{cd.get('protection_enabled')}`\n"
            f"Auto-Buy: `{cd.get('auto_buy')}`\n"
            f"Auto-Sell: `{cd.get('auto_sell')}`\n"
            f"Remove if no launch (h): `{cd.get('remove_if_no_launch_h')}`"
        )
        keyboard = [
            [InlineKeyboardButton(
                f"Keep Address: {'🟢' if cd.get('keep_address') else '🔴'}",
                callback_data=f"togglechild_{self._sid(address)}_keep_address",
            )],
            [InlineKeyboardButton(
                f"Protection ON: {'🟢' if cd.get('protection_enabled') else '🔴'}",
                callback_data=f"togglechild_{self._sid(address)}_protection_enabled",
            )],
            [InlineKeyboardButton(
                f"Auto-Buy: {'🟢' if cd.get('auto_buy') else '🔴'}",
                callback_data=f"togglechild_{self._sid(address)}_auto_buy",
            )],
            [InlineKeyboardButton(
                f"Auto-Sell: {'🟢' if cd.get('auto_sell') else '🔴'}",
                callback_data=f"togglechild_{self._sid(address)}_auto_sell",
            )],
            [InlineKeyboardButton("✏️ Remove if no launch (h)", callback_data=f"editchildh_{self._sid(address)}")],
            [InlineKeyboardButton("← Back", callback_data=f"protection_{self._sid(address)}")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_protection_mode(self, query, address: str):
        s = self.data_store.get_wallet_settings(address)
        current = s.get("protection_mode", "last_transfer")

        def mark(m):
            return "✅ " if current == m else ""

        text = "🔀 *Mode de Protection*"
        keyboard = [
            [InlineKeyboardButton(f"{mark('last_transfer')}📤 Last Transfer", callback_data=f"setprotmode_{self._sid(address)}_last_transfer")],
            [InlineKeyboardButton(f"{mark('exchange_pattern')}📊 Exchange Pattern", callback_data=f"setprotmode_{self._sid(address)}_exchange_pattern")],
            [InlineKeyboardButton("← Back", callback_data=f"protection_{self._sid(address)}")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_max_loss_counter(self, query, address: str):
        lang = self.data_store.state.get("language", "fr")
        s = self.data_store.get_wallet_settings(address)
        text = (
            f"{t('maxloss_title', lang)}\n\n"
            f"{t('current_threshold', lang)}: `{s.get('max_consecutive_losses')}` {t('consecutive_losses', lang)}\n"
            f"{t('current_counter', lang)}: `{s.get('consecutive_losses', 0)}`\n"
            f"{t('status_label', lang)}: {t('status_paused', lang) if s.get('paused') else t('status_active', lang)}"
        )
        keyboard = [
            [InlineKeyboardButton(t("btn_edit_threshold", lang), callback_data=f"editset_{self._sid(address)}|max_consecutive_losses|int")],
        ]
        if s.get("paused"):
            keyboard.append([InlineKeyboardButton(t("btn_reactivate_autobuy", lang), callback_data=f"unpause_{self._sid(address)}")])
        keyboard.append([InlineKeyboardButton(t("btn_back", lang), callback_data=f"rugger_{self._sid(address)}")])
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_security_ai_config(self, query, address: str):
        lang = self.data_store.state.get("language", "fr")
        s = self.data_store.get_wallet_settings(address)
        goplus_status = t("goplus_configured", lang) if config.GOPLUS_API_KEY else t("goplus_not_configured", lang)
        ai_status = (
            "✅ Claude" if config.ANTHROPIC_API_KEY
            else "✅ Grok" if config.GROK_API_KEY
            else t("ai_no_key", lang)
        )
        text = (
            f"{t('security_ai_title', lang)}\n\n"
            f"{t('goplus_check_label', lang)} : {goplus_status}\n"
            f"{t('enabled_on_rugger', lang)}: `{s.get('security_check_enabled')}`\n"
            f"{t('min_score_label', lang)}: `{s.get('min_security_score')}`/100\n\n"
            f"{t('ai_advice_label', lang)} : {ai_status}\n"
            f"{t('enabled_on_rugger_m', lang)}: `{s.get('ai_advisor_enabled')}`\n"
            f"{t('ai_advice_note', lang)}"
        )
        keyboard = [
            [InlineKeyboardButton(
                f"GoPlus: {'🟢' if s.get('security_check_enabled') else '🔴'}",
                callback_data=f"toggleset_{self._sid(address)}_security_check_enabled",
            )],
            [InlineKeyboardButton(t("btn_min_security_score", lang), callback_data=f"editset_{self._sid(address)}|min_security_score|float")],
            [InlineKeyboardButton(
                f"{t('ai_advice_label', lang).strip('*')}: {'🟢' if s.get('ai_advisor_enabled') else '🔴'}",
                callback_data=f"toggleset_{self._sid(address)}_ai_advisor_enabled",
            )],
            [InlineKeyboardButton(t("btn_back", lang), callback_data=f"rugger_{self._sid(address)}")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_snipe_config(self, query, address: str):
        """
        Reproduit la mise en page "nouvelle tâche de snipe" de F Project.
        Les réglages d'exécution (frais, tip, anti-MEV, slippage) sont SIMULÉS
        en PAPER — voir config.DEFAULT_WALLET_SETTINGS pour le détail.
        Les filtres dev (achat / détention) sont RÉELLEMENT vérifiés à l'achat.
        """
        entry = self.data_store.state["monitored_dev_wallets"].get(address, {})
        s = self.data_store.get_wallet_settings(address)
        label = entry.get("label", address[:8] + "...")
        platforms_display = ", ".join(s.get("platforms", ["pump_fun"]))
        lang = self.data_store.state.get("language", "fr")

        text = (
            f"{t('snipe_config_title', lang)} — `{label}`\n\n"
            f"{t('wallet_paper_line', lang)}\n"
            f"{t('label_line', lang)} : `{label}`\n"
            f"{t('snipe_amount_line', lang)} : `{s.get('buy_amount_sol', 0.1)}` SOL\n\n"
            f"{t('simulated_settings_note', lang)}\n"
            f"{t('gas_fee_line', lang)} : `{s.get('gas_fee_sol', 0.0025)}` SOL\n"
            f"{t('snipe_tip_line', lang)} : `{s.get('snipe_tip_sol', 0.005)}` SOL\n"
            f"{t('anti_mev_buy_line', lang)} : `{s.get('anti_mev_enabled', False)}`\n"
            f"{t('buy_slippage_line', lang)} : `{s.get('buy_slippage_pct', 50)}`%\n"
            f"{t('buy_slippage_pump_line', lang)} : `{s.get('buy_slippage_pump_pct', 50)}`%\n"
            f"{t('platforms_line', lang)} : `{platforms_display}`\n\n"
            f"{t('real_filters_note', lang)}\n"
            f"{t('dev_buy_minmax_line', lang)} : `{s.get('dev_buy_min_sol') or '-'}` / `{s.get('dev_buy_max_sol') or '-'}` SOL\n"
            f"{t('dev_holding_minmax_line', lang)} : `{s.get('dev_holding_min_pct') or '-'}` / `{s.get('dev_holding_max_pct') or '-'}` %\n\n"
            f"{t('speed_settings_note', lang)}\n"
            f"{t('jito_line', lang)} : `{s.get('use_jito')}` (global: `{config.JITO_ENABLED}`)\n"
            f"{t('max_speed_mode_line', lang)} : `{s.get('skip_price_check_for_speed')}` "
            f"{t('max_speed_mode_note', lang)}"
        )

        keyboard = [
            [InlineKeyboardButton(t("btn_snipe_amount", lang), callback_data=f"editset_{self._sid(address)}|buy_amount_sol|float")],
            [InlineKeyboardButton(t("btn_gas_fee", lang), callback_data=f"editset_{self._sid(address)}|gas_fee_sol|float")],
            [InlineKeyboardButton(t("btn_snipe_tip", lang), callback_data=f"editset_{self._sid(address)}|snipe_tip_sol|float")],
            [InlineKeyboardButton(
                f"{t('btn_anti_mev', lang)}: {'🟢' if s.get('anti_mev_enabled') else '🔴'}",
                callback_data=f"toggleset_{self._sid(address)}_anti_mev_enabled",
            )],
            [InlineKeyboardButton(t("btn_buy_slippage", lang), callback_data=f"editset_{self._sid(address)}|buy_slippage_pct|float")],
            [InlineKeyboardButton(t("btn_buy_slippage_pump", lang), callback_data=f"editset_{self._sid(address)}|buy_slippage_pump_pct|float")],
            [InlineKeyboardButton(
                f"{t('btn_jito', lang)}: {'🟢' if s.get('use_jito') else '🔴'}",
                callback_data=f"toggleset_{self._sid(address)}_use_jito",
            )],
            [InlineKeyboardButton(
                f"{t('btn_max_speed_mode', lang)}: {'🟢' if s.get('skip_price_check_for_speed') else '🔴'}",
                callback_data=f"toggleset_{self._sid(address)}_skip_price_check_for_speed",
            )],
            [InlineKeyboardButton(t("btn_dev_buy_min", lang), callback_data=f"editset_{self._sid(address)}|dev_buy_min_sol|float|none")],
            [InlineKeyboardButton(t("btn_dev_buy_max", lang), callback_data=f"editset_{self._sid(address)}|dev_buy_max_sol|float|none")],
            [InlineKeyboardButton(t("btn_dev_holding_min", lang), callback_data=f"editset_{self._sid(address)}|dev_holding_min_pct|float|none")],
            [InlineKeyboardButton(t("btn_dev_holding_max", lang), callback_data=f"editset_{self._sid(address)}|dev_holding_max_pct|float|none")],
            [InlineKeyboardButton(t("btn_back", lang), callback_data=f"rugger_{self._sid(address)}")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    # ══════════════════════════════════════════════════════════
    # ⚙️ SETTINGS
    # ══════════════════════════════════════════════════════════

    async def show_settings_menu(self, query):
        lang = self.data_store.state.get("language", "fr")
        ai_status = "🟢 ON" if self.data_store.is_ai_enabled_globally() else "🔴 OFF"
        text = t("settings_title", lang)
        keyboard = [
            [InlineKeyboardButton(t("btn_gas_fees", lang), callback_data="gas_fees")],
            [InlineKeyboardButton(t("btn_notifications", lang), callback_data="notif_settings")],
            [InlineKeyboardButton(f"{t('global_ai_label', lang)}: {ai_status}", callback_data="toggleglobalai")],
            [InlineKeyboardButton("🧹 Nettoyage automatique", callback_data="cleanup_settings")],
            [InlineKeyboardButton("📤 Alerte retrait SOL", callback_data="withdrawal_settings")],
            [InlineKeyboardButton("💸 Alerte transfert dev (90%)", callback_data="devtransfer_settings")],
            [InlineKeyboardButton(t("btn_back", lang), callback_data="menu_main")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_withdrawal_settings(self, query):
        """AJOUTÉ suite à une demande explicite : réglages de l'alerte
        retrait SOL (montant absolu, tous wallets surveillés).
        ÉTENDU (2e fois) avec auto_add et allow_cascade — voir main.py
        on_wallet_withdrawal pour la logique anti-cascade.
        ÉTENDU (3e fois) avec le seuil USDC — voir copytrade_listener.py
        _check_withdrawal, désormais aussi actif sur les transferts USDC."""
        settings = self.data_store.get_withdrawal_alert_settings()
        status_icon = "🟢 ON" if settings["enabled"] else "🔴 OFF"
        auto_add_icon = "🟢 ON" if settings.get("auto_add", True) else "🔴 OFF"
        cascade_icon = "🟢 ON" if settings.get("allow_cascade", False) else "🔴 OFF"
        text = (
            f"📤 *Alerte retrait SOL + USDC*\n\n"
            f"Alerte quand N'IMPORTE QUEL wallet surveillé (Ruggeur ou Copy Trading) "
            f"envoie du SOL ou de l'USDC vers une autre adresse.\n\n"
            f"Statut : {status_icon}\n"
            f"Montant minimum SOL : `{settings['min_sol']:.4f}` SOL\n"
            f"Montant minimum USDC : `{settings.get('min_usdc', 20):.2f}` USDC\n"
            f"Ajout automatique de l'adresse destinataire : {auto_add_icon}\n"
            f"Autoriser la cascade (adresse ajoutée → peut elle-même déclencher un ajout) : {cascade_icon}"
        )
        keyboard = [
            [InlineKeyboardButton(f"Activé/Désactivé : {status_icon}", callback_data="withdrawal_toggle")],
            [InlineKeyboardButton("✏️ Modifier le montant minimum (SOL)", callback_data="withdrawal_edit_min_sol")],
            [InlineKeyboardButton("✏️ Modifier le montant minimum (USDC)", callback_data="withdrawal_edit_min_usdc")],
            [InlineKeyboardButton(f"Ajout auto destinataire : {auto_add_icon}", callback_data="withdrawal_toggle_autoadd")],
            [InlineKeyboardButton(f"Autoriser la cascade : {cascade_icon}", callback_data="withdrawal_toggle_cascade")],
            [InlineKeyboardButton(t("btn_back", self.data_store.state.get("language", "fr")), callback_data="menu_settings")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_dev_transfer_settings(self, query):
        """AJOUTÉ suite à une demande explicite : réglages du système à
        90% (transfert SOL important d'un DEV surveillé) — n'avait aucun
        menu Telegram jusqu'ici, seulement des variables d'environnement."""
        settings = self.data_store.get_dev_transfer_settings()
        auto_add_icon = "🟢 ON" if settings.get("auto_add", True) else "🔴 OFF"
        cascade_icon = "🟢 ON" if settings.get("allow_cascade", False) else "🔴 OFF"
        text = (
            f"💸 *Alerte transfert SOL important (devs)*\n\n"
            f"Alerte quand un DEV surveillé (Ruggeur) transfère une grosse "
            f"partie de son solde SOL — signal qu'il encaisse et se prépare à disparaître.\n\n"
            f"Seuil : `{settings['pct_threshold']:.0f}%` du solde\n"
            f"Ajout automatique de l'adresse destinataire : {auto_add_icon}\n"
            f"Autoriser la cascade : {cascade_icon}"
        )
        keyboard = [
            [InlineKeyboardButton("✏️ Modifier le seuil (%)", callback_data="devtransfer_edit_pct")],
            [InlineKeyboardButton(f"Ajout auto destinataire : {auto_add_icon}", callback_data="devtransfer_toggle_autoadd")],
            [InlineKeyboardButton(f"Autoriser la cascade : {cascade_icon}", callback_data="devtransfer_toggle_cascade")],
            [InlineKeyboardButton(t("btn_back", self.data_store.state.get("language", "fr")), callback_data="menu_settings")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_cleanup_settings(self, query):
        """
        AJOUTÉ suite à une demande explicite : réglages du nettoyage
        automatique (wallet_cleanup.py) — inactivité max et pertes
        consécutives max, modifiables ici sans toucher au code.
        """
        settings = self.data_store.get_cleanup_settings()
        status_icon = "🟢 ON" if settings["enabled"] else "🔴 OFF"
        text = (
            f"🧹 *Nettoyage automatique*\n\n"
            f"Retire automatiquement du monitoring les wallets inactifs ou en série de pertes.\n\n"
            f"Statut : {status_icon}\n"
            f"Inactivité max : `{settings['inactive_days']:.0f}` jour(s)\n"
            f"Pertes consécutives max : `{settings['max_consecutive_losses']}`"
        )
        keyboard = [
            [InlineKeyboardButton(f"Activé/Désactivé : {status_icon}", callback_data="cleanup_toggle")],
            [InlineKeyboardButton("✏️ Modifier l'inactivité max (jours)", callback_data="cleanup_edit_inactive_days")],
            [InlineKeyboardButton("✏️ Modifier les pertes consécutives max", callback_data="cleanup_edit_max_losses")],
            [InlineKeyboardButton(t("btn_back", self.data_store.state.get("language", "fr")), callback_data="menu_settings")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_gas_fees(self, query):
        lang = self.data_store.state.get("language", "fr")
        text = (
            f"{t('gas_fees_title', lang)}\n\n"
            f"{t('gas_fees_note', lang)}\n\n"
            f"{t('gas_fees_summary', lang)}"
        )
        keyboard = [[InlineKeyboardButton(t("btn_back", lang), callback_data="menu_settings")]]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_notification_settings(self, query):
        lang = self.data_store.state.get("language", "fr")
        prefs = self.data_store.get_notification_prefs()
        labels = {
            "buy_confirmed": "📦 Buy Confirmed",
            "sell_success": "📦 Sell Success",
            "rugger_alert": "🛡️ Rugger Alert",
            "buy_skipped": "📋 Buy Skipped",
            "processing_buy": "📋 Processing Buy",
        }
        text = f"{t('notifications_title', lang)}\n\n{t('notifications_always_on', lang)}"
        keyboard = []
        for key, label in labels.items():
            state_icon = "🟢" if prefs.get(key) else "🔴"
            keyboard.append([InlineKeyboardButton(f"{label}: {state_icon}", callback_data=f"togglenotif_{key}")])
        keyboard.append([InlineKeyboardButton(t("btn_back", lang), callback_data="menu_settings")])
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    # ══════════════════════════════════════════════════════════
    # 📊 POSITIONS
    # ══════════════════════════════════════════════════════════

    async def show_positions(self, query):
        lang = self.data_store.state.get("language", "fr")
        positions = self.data_store.state.get("open_positions", [])
        if not positions:
            text = f"{t('positions_title', lang)}\n\n{t('no_open_positions', lang)}"
            keyboard = [
                [InlineKeyboardButton(t("btn_refresh", lang), callback_data="menu_positions")],
                [InlineKeyboardButton(t("btn_back", lang), callback_data="menu_main")],
            ]
            await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)
            return

        lines = [f"{t('positions_title', lang)}\n"]
        keyboard = []
        for i, pos in enumerate(positions):
            lines.append(
                t("position_line", lang, mint=pos["token_mint"][:8], price=pos["entry_price"],
                  units=pos["units"], cost=pos["cost_basis_usd"])
            )
            keyboard.append([
                InlineKeyboardButton("25%", callback_data=f"sell_{i}_25"),
                InlineKeyboardButton("50%", callback_data=f"sell_{i}_50"),
                InlineKeyboardButton("75%", callback_data=f"sell_{i}_75"),
                InlineKeyboardButton("100%", callback_data=f"sell_{i}_100"),
            ])
            keyboard.append([InlineKeyboardButton(t("btn_sell_initials", lang, n=i + 1), callback_data=f"sellinitials_{i}")])

        keyboard.append([InlineKeyboardButton(t("btn_sell_all", lang), callback_data="asksellall")])
        keyboard.append([InlineKeyboardButton(t("btn_refresh", lang), callback_data="menu_positions")])
        keyboard.append([InlineKeyboardButton(t("btn_back", lang), callback_data="menu_main")])
        await self._send_or_edit(query, "\n".join(lines), InlineKeyboardMarkup(keyboard), edit=True)

    # ══════════════════════════════════════════════════════════
    # ••• MORE
    # ══════════════════════════════════════════════════════════

    async def analyze_coin_inline(self, update: Update, token_mint: str):
        """
        Version Telegram de find_dev.py + analyze_yield.py — analyse un token
        directement dans le chat (au lieu de lancer un script séparé).
        Lecture seule : ne fait aucun achat, ne modifie rien du monitoring.
        """
        import find_dev
        import fund_tracer
        import wallet_history
        import backtest

        msg = await update.message.reply_text(f"🔍 Analyse de `{token_mint[:12]}...` en cours...", parse_mode="Markdown")

        if not config.HELIUS_API_KEY:
            await msg.edit_text("❌ Aucune clé HELIUS_API_KEY configurée.")
            return

        creator_info = await find_dev.find_token_creator(token_mint)
        if not creator_info:
            await msg.edit_text("❌ Impossible de trouver le créateur de ce token (historique introuvable ou erreur réseau).")
            return

        dev = creator_info["dev_address"]
        confidence = "✅ confirmé" if creator_info["verified"] else "⚠️ non confirmé"

        # CORRIGÉ suite à une demande explicite : cette fonction n'affichait
        # ni le MONTANT du financement, ni le statut FRESH WALLET, ni la
        # liste des tokens déjà créés — alors que ces infos étaient déjà
        # disponibles (trace["amount_sol"] vient de fund_tracer.classify_scheme,
        # qui appelle get_first_funder en interne) ou faciles à ajouter
        # (fund_tracer.is_fresh_wallet). "Analyse de wallet" affichait déjà
        # tout ça — mise en cohérence des deux fonctions.
        trace = await fund_tracer.classify_scheme(dev)
        is_fresh = await fund_tracer.is_fresh_wallet(dev)
        past_tokens = await wallet_history.get_created_tokens(dev, max_results=10)

        text = (
            f"🔍 *Analyse du token*\n`{token_mint}`\n\n"
            f"👤 *Dev* : `{dev}`\n_Fiabilité : {confidence}_\n\n"
        )
        if trace.get("funder_address"):
            text += f"🔗 *Financement* : `{trace.get('scheme', 'inconnu')}`"
            if trace.get("exchange_name"):
                text += f" ({trace['exchange_name']})"
            text += f"\nFinancé par : `{trace['funder_address'][:12]}...`\nMontant : `{_format_sol_amount(trace.get('amount_sol', 0))}` SOL\n"
        else:
            text += "🔗 *Financement* : introuvable\n"
        text += f"🆕 Fresh wallet : {'✅ Oui' if is_fresh else '❌ Non (déjà actif avant)'}\n\n"

        text += f"📜 *Historique* : `{len(past_tokens)}` token(s) créé(s) précédemment (sur les 10 derniers scannés)\n"

        # Liste détaillée token par token — AJOUTÉ, affichée même en dessous
        # du seuil de 3 (le backtest agrégé ci-dessous reste réservé à
        # ≥3 pour rester statistiquement fiable, mais voir CE que le dev a
        # créé reste utile même avec 1-2 tokens seulement).
        if past_tokens:
            for i, pt in enumerate(past_tokens):
                if len(past_tokens) > 1:
                    try:
                        await msg.edit_text(
                            f"🔍 Analyse de `{token_mint[:12]}...` en cours...\n\n"
                            f"📜 Détail de l'historique : `{i + 1}/{len(past_tokens)}`...",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
                pair_data = await backtest._get_pair_data(pt["token_mint"])
                symbol = (pair_data.get("baseToken") or {}).get("symbol")
                url = pair_data.get("url")
                mc = pair_data.get("marketCap") or pair_data.get("fdv")
                # CORRIGÉ suite à un vrai signalement : cette liste n'utilisait
                # QUE DexScreener pour le nom/MC — qui n'a aucune donnée pour
                # un token encore sur la bonding curve (jamais migré), càd la
                # quasi-totalité des tokens créés par un dev ordinaire. Repli
                # sur la lecture bonding curve on-chain (même méthode que
                # paper_trader.py pour le prix d'achat) quand DexScreener est
                # vide — au moins le market cap redevient disponible, même
                # sans nom de token (pas dans la bonding curve elle-même).
                if not mc:
                    onchain = await backtest.get_bonding_curve_price(pt["token_mint"])
                    if onchain and not onchain.get("complete"):
                        mc = onchain.get("market_cap_usd")
                if symbol and url:
                    name_display = f"[{symbol}]({url})"
                elif symbol:
                    name_display = f"{symbol} — `{pt['token_mint'][:8]}...`"
                else:
                    name_display = f"`{pt['token_mint'][:8]}...`"
                mc_display = f" (MC actuel: ${mc:,.0f})" if mc else " (données indisponibles)"
                text += f"   • {name_display}{mc_display}\n"

        if len(past_tokens) >= 3:
            result = await backtest.backtest_wallet(past_tokens)
            regularity = await wallet_history.check_sell_regularity(dev, past_tokens)
            verdict = "✅ Correspond aux critères" if (result["should_monitor"] and regularity["regularity_score"] >= 0.3) else "❌ Ne correspond pas aux critères"

            text += (
                f"\n📊 *Backtest* :\n"
                f"Résultat cumulé : `{result['total_result_pct']:+.1f}%`\n"
                f"Ratio gain/perte : `{result['ratio']:.2f}`\n"
                f"TP suggéré : `{result['suggested_tp_pct']:.0f}%`\n"
                f"Score de régularité : `{regularity['regularity_score']:.2f}`\n\n"
                f"*{verdict}*"
            )
        else:
            text += "\n⏭️ _Historique insuffisant (< 3 tokens) pour un backtest fiable._"

        # AJOUTÉ suite à une demande explicite : "Analyse du token" ne
        # faisait que 2 des 4 étapes de find_dev_cluster.py — elle identifiait
        # QUE le schéma était "exchange" sans jamais chercher les AUTRES
        # wallets financés par le même montant depuis la même adresse
        # (la méthode "adresse intermédiaire d'exchange" elle-même). Branche
        # maintenant find_cluster_addresses() ici, avec retour de
        # progression Telegram (cette recherche peut prendre plusieurs
        # minutes sur une adresse d'exchange à fort volume).
        cluster_info = None
        if trace.get("scheme") == "exchange":
            import find_dev_cluster

            def _cluster_progress(progress_text: str):
                async def _safe_edit():
                    try:
                        await msg.edit_text(
                            f"🔍 Analyse de `{token_mint[:12]}...` en cours...\n\n"
                            f"🔗 *Recherche du cluster d'adresses liées* (méthode adresse intermédiaire) :\n{progress_text}",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
                asyncio.create_task(_safe_edit())

            cluster_info = await find_dev_cluster.find_cluster_addresses(dev, max_starred=12, on_progress=_cluster_progress)
            if cluster_info and len(cluster_info.get("cluster", [])) > 1:
                self.data_store.state.setdefault("cluster_cache", {})[dev] = cluster_info["cluster"]
                if cluster_info.get("funder"):
                    self.data_store.state.setdefault("protection_target_cache", {})[cluster_info["funder"]] = {
                        "amount_sol": trace.get("amount_sol"),
                        "label": f"exchange_{cluster_info['funder'][:8]}",
                    }
                self.data_store.save()

        if cluster_info and len(cluster_info.get("cluster", [])) > 1:
            starred = cluster_info.get("starred_info", [])
            text += (
                f"\n\n🔗 *Cluster d'adresses liées* (financées par le même exchange, "
                f"même montant ±0.001%)\n"
                f"Financeur commun : `{cluster_info['funder'][:12]}...`\n"
                f"Adresses ⭐ trouvées (ont déjà créé un token) : `{len(starred)}`\n"
            )
            # CORRIGÉ suite à une demande explicite : n'affichait que les
            # adresses brutes tronquées (ex: "⭐ 5gWrKGAQ... → HA2q9y6r..."),
            # illisible. Calcule maintenant le vrai résultat de CHAQUE token
            # créé par chaque adresse ⭐ (même méthode "MC max après entrée"
            # que partout ailleurs dans le bot), avec nom cliquable vers
            # DexScreener au lieu de l'adresse du mint.
            # RÉDUIT suite à une vraie surcharge Helius observée en
            # conditions réelles (vague d'erreurs "Internal error"/timeout
            # juste après l'ajout de cette fonctionnalité) : 12 adresses ×
            # 5 tokens chacune pouvait déclencher des dizaines d'appels
            # getTransaction supplémentaires d'un coup. Réduit à 6 adresses
            # × 3 tokens — toujours utile, beaucoup moins agressif.
            for i, s in enumerate(starred[:6]):
                if len(starred) > 1:
                    try:
                        await msg.edit_text(
                            f"🔍 Analyse de `{token_mint[:12]}...` en cours...\n\n"
                            f"🔗 Calcul des résultats du cluster : `{i + 1}/{min(len(starred), 6)}`...",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
                addr_tokens = await wallet_history.get_created_tokens(s["address"], max_results=3)
                text += f"   ⭐ `{s['address'][:10]}...`\n"
                if not addr_tokens:
                    text += f"      _(token {s['mint'][:10]}... — détails indisponibles)_\n"
                    continue
                for ct in addr_tokens:
                    detail = await backtest.get_detailed_trade_info(
                        ct["token_mint"], purchase_block_time=ct.get("block_time"),
                    )
                    status_icon = "✅" if detail["hit_tp"] else ("❌" if detail["hit_sl"] else "➖")
                    if detail.get("symbol") and detail.get("dexscreener_url"):
                        name_display = f"[{detail['symbol']}]({detail['dexscreener_url']})"
                    elif detail.get("symbol"):
                        name_display = f"{detail['symbol']} — `{ct['token_mint'][:8]}...`"
                    else:
                        name_display = f"`{ct['token_mint'][:8]}...`"
                    mc_display = f", MC entrée: ${detail['entry_market_cap_usd']:,.0f}" if detail.get("entry_market_cap_usd") else ""
                    text += f"      {status_icon} {name_display} → `{detail['result_pct']:+.1f}%` ({detail['purchase_date']}{mc_display})\n"
            if cluster_info.get("neutral"):
                text += f"_+ {len(cluster_info['neutral'])} adresse(s) matchées sans création connue (ignorées)_\n"

            # Backtest sur les 10 tokens les plus récents à travers TOUT le
            # cluster (pas juste l'adresse de départ) — reproduit l'étape
            # 2/3 + 3/3 de find_dev_cluster.py, condensé pour Telegram.
            try:
                await msg.edit_text(
                    f"🔍 Analyse de `{token_mint[:12]}...` en cours...\n\n"
                    f"🔗 Cluster trouvé ({len(cluster_info['cluster'])} adresse(s)) — "
                    f"backtest sur les tokens les plus récents...",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

            all_cluster_tokens = []
            for addr in cluster_info["cluster"]:
                addr_tokens = await wallet_history.get_created_tokens(addr, max_results=10)
                for ct in addr_tokens:
                    ct["creator_address"] = addr
                all_cluster_tokens.extend(addr_tokens)

            recent_cluster_tokens = sorted(
                all_cluster_tokens, key=lambda t: t.get("block_time") or -1, reverse=True
            )[:10]

            if len(recent_cluster_tokens) >= 3:
                cluster_result = await backtest.backtest_wallet(recent_cluster_tokens)
                cluster_verdict = "✅ Correspond aux critères" if cluster_result["should_monitor"] else "❌ Ne correspond pas aux critères"
                text += (
                    f"\n📊 *Backtest du cluster* (`{len(recent_cluster_tokens)}` tokens les plus récents, "
                    f"toutes adresses confondues) :\n"
                    f"Résultat cumulé : `{cluster_result['total_result_pct']:+.1f}%`\n"
                    f"Ratio gain/perte : `{cluster_result['ratio']:.2f}`\n\n"
                    f"*{cluster_verdict}*"
                )
            else:
                text += "\n⏭️ _Historique du cluster insuffisant (< 3 tokens au total) pour un backtest fiable._"

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard_rows = [[InlineKeyboardButton("➕ Ajouter ce dev en Ruggeur", callback_data=f"quickadddev_{dev}")]]
        if cluster_info and len(cluster_info.get("cluster", [])) > 1:
            keyboard_rows.append([InlineKeyboardButton(
                f"➕ Ajouter tout le cluster ({len(cluster_info['cluster'])}) en Ruggeur",
                callback_data=f"quickaddcluster_{dev}",
            )])
            # AJOUTÉ suite à une question explicite : le scanner de
            # protection (protection_scanner.py) surveille en continu les
            # adresses exchange enregistrées, pour détecter automatiquement
            # les FUTURS wallets financés par le même montant — exactement
            # la méthode "adresse intermédiaire". Mais rien nulle part dans
            # ce fichier n'appelait jamais add_dev_wallet... add_protection_target()
            # pour remplir cette liste — le scanner tournait, mais surveillait
            # une liste toujours vide. Ce bouton ferme cette boucle : le
            # financeur trouvé ici devient une cible surveillée en
            # permanence, pas juste un cluster figé au moment du scan.
            if cluster_info.get("funder"):
                keyboard_rows.append([InlineKeyboardButton(
                    "🛡️ Surveiller cet exchange en continu (nouveaux wallets)",
                    callback_data=f"quickaddprotection_{cluster_info['funder']}",
                )])
        keyboard_rows.append([InlineKeyboardButton("← Back", callback_data="menu_main")])
        keyboard = InlineKeyboardMarkup(keyboard_rows)
        # CORRIGÉ suite à un vrai bug signalé plusieurs fois ("rien ne vient
        # depuis") : cet edit_text final n'avait AUCUN filet de sécurité. Si
        # Telegram refusait le Markdown (caractère spécial dans un nom de
        # token, ex: un underscore non échappé) ou appliquait une limite de
        # fréquence (après les nombreux messages de progression envoyés
        # pendant la recherche de cluster), l'exception partait sans être
        # rattrapée — le handler s'arrêtait en silence, sans jamais rien
        # afficher à l'utilisateur, alors que tout le calcul avait pourtant
        # bien fini de tourner côté serveur.
        try:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard, disable_web_page_preview=True)
        except Exception as e:
            log.warning(f"Erreur d'affichage Markdown pour l'analyse de {token_mint}: {e}")
            try:
                # Repli : même contenu, sans aucun formatage Markdown — ne
                # peut plus jamais échouer pour une histoire de caractère
                # spécial mal échappé.
                plain_text = text.replace("*", "").replace("`", "").replace("_", "")
                await msg.edit_text(plain_text, reply_markup=keyboard, disable_web_page_preview=True)
            except Exception as e2:
                log.error(f"Échec du repli texte brut pour {token_mint}: {e2}")
                await msg.edit_text("❌ Erreur d'affichage — relance l'analyse.")

    async def show_pnl_journal(self, query, page: int = 0):
        """Journal P&L — historique navigable des positions clôturées."""
        closed = list(reversed(self.data_store.state.get("closed_positions", [])))  # plus récent en premier
        page_size = 5
        start = page * page_size
        page_items = closed[start:start + page_size]

        if not closed:
            text = "📋 *Journal P&L*\n\nAucun trade clôturé pour l'instant."
        else:
            lines = [f"📋 *Journal P&L* ({len(closed)} trade(s) au total)\n"]
            for pos in page_items:
                emoji = "✅" if pos.get("total_sol_received", 0) >= pos.get("total_sol_invested", 0) else "❌"
                result_pct = pos.get("result_pct", 0)
                pnl_sol = pos.get("total_sol_received", 0) - pos.get("total_sol_invested", 0)
                reason = pos.get("close_reason", "?")
                lines.append(
                    f"{emoji} `{pos['token_mint'][:8]}...` — {result_pct:+.1f}% "
                    f"({pnl_sol:+.3f} SOL) — {reason}"
                )
            text = "\n".join(lines)

        keyboard = []
        nav_row = []
        if start > 0:
            nav_row.append(InlineKeyboardButton("◀️", callback_data=f"journal_page_{page-1}"))
        if start + page_size < len(closed):
            nav_row.append(InlineKeyboardButton("▶️", callback_data=f"journal_page_{page+1}"))
        if nav_row:
            keyboard.append(nav_row)
        keyboard.append([InlineKeyboardButton("← Back", callback_data="menu_main")])
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_help(self, query):
        """Aide intégrée — explique chaque section du bot."""
        lang = self.data_store.state.get("language", "fr")
        text = t("help_text", lang)
        keyboard = [[InlineKeyboardButton(t("btn_back", lang), callback_data="menu_main")]]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def check_security_inline(self, update: Update, token_mint: str):
        """🛡️ Sécurité/Scam — vérification GoPlus directe, sans wallet à configurer."""
        import security
        msg = await update.message.reply_text(f"🛡️ Vérification sécurité de `{token_mint[:12]}...`...", parse_mode="Markdown")

        result = await security.check_token_security(token_mint)
        if not result:
            await msg.edit_text("❌ Impossible de récupérer les données de sécurité (pas de clé GoPlus configurée, ou erreur réseau).")
            return

        risk_icon = "🔴" if result.get("is_risky") else "🟢"
        text = (
            f"🛡️ *Analyse de sécurité*\n`{token_mint}`\n\n"
            f"{risk_icon} Risque global : {'Élevé' if result.get('is_risky') else 'Faible'}\n\n"
        )
        for flag_name, flag_value in result.get("flags", {}).items():
            icon = "⚠️" if flag_value else "✅"
            text += f"{icon} {flag_name}\n"

        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="menu_main")]])
        # CORRIGÉ — même filet de sécurité qu'ailleurs : flag_name vient de
        # l'API GoPlus et contient souvent des underscores (ex:
        # "is_honeypot") non échappés, qui cassent le Markdown Telegram
        # sans prévenir si jamais rencontrés.
        try:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard, disable_web_page_preview=True)
        except Exception as e:
            log.warning(f"Erreur d'affichage Markdown pour la sécurité de {token_mint}: {e}")
            plain_text = text.replace("*", "").replace("`", "").replace("_", "")
            await msg.edit_text(plain_text, reply_markup=keyboard, disable_web_page_preview=True)

    async def analyze_wallet_inline(self, update: Update, wallet_address: str):
        """
        🔎 Analyse de wallet — MODIFIÉ (demande explicite, 19 août) : affiche
        maintenant un résumé RAPIDE d'abord (financement + fresh wallet,
        toujours calculés nous-mêmes, + résumé GMGN si disponible — 2 appels
        API légers, pas de RPC), avant de proposer le détail complet
        (reconstruction on-chain token par token) comme une action SÉPARÉE,
        à la demande, via le bouton "🔍 Voir le détail complet".

        Avant ce changement, le détail complet (jusqu'à 10 tokens décodés
        on-chain, potentiellement plusieurs minutes) se lançait
        AUTOMATIQUEMENT à chaque "Analyse de wallet" — coûteux en RPC même
        pour juste vérifier rapidement un wallet. Voir
        analyze_wallet_detail_inline pour la suite (ex-contenu de cette
        fonction, inchangé).
        """
        import fund_tracer
        import gmgn_client

        msg = await update.message.reply_text(f"🔎 Analyse du wallet `{wallet_address[:12]}...`...", parse_mode="Markdown")

        if not config.HELIUS_API_KEY:
            await msg.edit_text("❌ Aucune clé HELIUS_API_KEY configurée.")
            return

        # ── Financement + fresh wallet (toujours nous-mêmes, rapide) ─────
        trace = await fund_tracer.get_first_funder(wallet_address)
        is_fresh = await fund_tracer.is_fresh_wallet(wallet_address)

        text = f"🔎 *Analyse du wallet*\n`{wallet_address}`\n\n"

        if trace.get("funder_address"):
            text += f"💰 Financé par : `{trace['funder_address'][:12]}...`"
            if trace.get("exchange_name"):
                text += f" ({trace['exchange_name']})"
            text += f"\nMontant : `{_format_sol_amount(trace.get('amount_sol', 0))}` SOL\nSchéma : `{trace.get('scheme', 'inconnu')}`\n"
            if trace.get("funder_behavior_labels"):
                text += f"Labels comportementaux : `{', '.join(trace['funder_behavior_labels'])}`\n"
        else:
            text += "💰 Financement : introuvable\n"

        text += f"🆕 Fresh wallet : {'✅ Oui' if is_fresh else '❌ Non (déjà actif avant)'}\n\n"

        # ── Résumé GMGN (rapide — win rate, profit, tags, sans RPC) ──────
        gmgn_stats = await gmgn_client.get_wallet_stats(wallet_address)
        gmgn_profits_resp = await gmgn_client.get_wallet_profits(wallet_address)
        gmgn_profits_list = gmgn_profits_resp.get("list") if gmgn_profits_resp else None
        gmgn_profits = gmgn_profits_list[0] if gmgn_profits_list else None

        pnl_stat = gmgn_stats.get("pnl_stat") if gmgn_stats else None
        has_gmgn_data = bool(pnl_stat and pnl_stat.get("token_num", 0) > 0)

        if has_gmgn_data:
            winrate_pct = pnl_stat.get("winrate", 0) * 100
            common = gmgn_stats.get("common") or {}
            tags = common.get("tags") or []
            tags_display = f" — étiquettes GMGN : {', '.join(tags)}" if tags else ""

            text += (
                f"🌐 *Résumé GMGN* (source externe, {pnl_stat['token_num']} token(s) connus)"
                f"{tags_display}\n"
                f"   Win rate : `{winrate_pct:.0f}%`\n"
            )
            if gmgn_profits:
                try:
                    total_profit = float(gmgn_profits.get("total_profit", 0) or 0)
                    total_cost = float(gmgn_profits.get("total_cost", 0) or 0)
                except (TypeError, ValueError):
                    total_profit, total_cost = 0.0, 0.0
                profit_pct = (total_profit / total_cost * 100) if total_cost else 0
                text += (
                    f"   Profit réalisé (7j) : `${total_profit:,.2f}` (`{profit_pct:+.1f}%`) — "
                    f"`{gmgn_profits.get('buy', 0)}` achat(s), `{gmgn_profits.get('sell', 0)}` vente(s)\n"
                )
            text += "\n"
        elif not config.GMGN_API_KEY:
            text += "🌐 _Résumé GMGN indisponible (GMGN_API_KEY non configurée)._\n\n"
        else:
            text += "🌐 _GMGN n'a aucune donnée sur ce wallet précis._\n\n"

        text += (
            "_Résumé rapide seulement — aucune reconstruction on-chain n'a été faite. "
            "Pour le détail token par token (plus lent), utilise le bouton ci-dessous._"
        )

        keyboard = [
            [InlineKeyboardButton("📋 Achats récents (10 tokens)", callback_data=f"walletbuys_{wallet_address}")],
            [InlineKeyboardButton("🔍 Voir le détail complet (plus lent)", callback_data=f"walletdetail_{wallet_address}")],
            [InlineKeyboardButton("← Back", callback_data="menu_main")],
        ]
        try:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
        except Exception as e:
            log.warning(f"Erreur d'affichage Markdown pour le résumé rapide de {wallet_address}: {e}")
            plain_text = text.replace("*", "").replace("`", "").replace("_", "")
            await msg.edit_text(plain_text, reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)

    async def analyze_wallet_buys_only_inline(self, query, wallet_address: str):
        """
        📋 Achats récents — AJOUTÉ (demande explicite, 19 août) : juste la
        liste des 10 derniers tokens différents achetés par ce wallet, avec
        résultat par token — SANS checklist copy trading, note en étoiles,
        score bot, ni potentiel ATH. Plus rapide que "détail complet" (évite
        toutes les analyses supplémentaires), mais reste plus lent que le
        résumé GMGN (la reconstruction on-chain par token reste nécessaire
        pour un vrai résultat par trade, GMGN ne le donne pas — voir
        gmgn_client.py et la discussion sur wallet_activity/wallet_profits).
        """
        import wallet_history
        import backtest

        await query.edit_message_text(f"📋 Achats récents de `{wallet_address[:12]}...`...", parse_mode="Markdown")

        if not config.HELIUS_API_KEY:
            await query.edit_message_text("❌ Aucune clé HELIUS_API_KEY configurée.")
            return

        recent_buys = await wallet_history.get_recent_buys(wallet_address, max_results=10)
        text = f"📋 *Achats récents — {wallet_address[:12]}...*\n\n"
        text += f"`{len(recent_buys)}` achat(s) Pump.fun trouvé(s) (sur les 10 derniers scannés)\n"

        trader_results = []
        if len(recent_buys) >= 1:
            for i, buy in enumerate(recent_buys):
                try:
                    await query.edit_message_text(
                        f"📋 Achats récents de `{wallet_address[:12]}...`\n\n"
                        f"Analyse : `{i + 1}/{len(recent_buys)}` en cours "
                        f"(scan on-chain — peut prendre jusqu'à 1-2 min par token)...",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

                detail = await backtest.get_detailed_trade_info(
                    buy["token_mint"], purchase_block_time=buy.get("block_time"), tp_pct=100.0, sl_pct=config.SL_PCT,
                    sol_spent=buy.get("sol_spent"), tokens_received=buy.get("tokens_received"),
                )

                max_mc_filter = config.DEFAULT_WALLET_SETTINGS.get("max_market_cap")
                entry_mc = detail.get("entry_market_cap_usd")
                excluded_by_filter = bool(max_mc_filter and entry_mc and entry_mc > max_mc_filter)

                if not excluded_by_filter:
                    trader_results.append(detail)
                status_icon = "🚫" if excluded_by_filter else ("✅" if detail["hit_tp"] else ("❌" if detail["hit_sl"] else "➖"))

                if detail.get("symbol") and detail.get("dexscreener_url"):
                    name_display = f"[{detail['symbol']}]({detail['dexscreener_url']})"
                elif detail.get("symbol"):
                    name_display = f"{detail['symbol']} — `{buy['token_mint'][:8]}...`"
                else:
                    name_display = f"`{buy['token_mint'][:8]}...`"

                mc_display = f", MC entrée: ${detail['entry_market_cap_usd']:,.0f}" if detail.get("entry_market_cap_usd") else ""
                filter_note = f" _(> filtre {max_mc_filter:,.0f}$, exclu)_" if excluded_by_filter else ""
                text += f"   {status_icon} {name_display} → `{detail['result_pct']:+.1f}%` ({detail['purchase_date']}{mc_display}){filter_note}\n"

        if len(trader_results) >= 3:
            avg = sum(r["result_pct"] for r in trader_results) / len(trader_results)
            wins = [r for r in trader_results if r["result_pct"] > 0]
            win_rate = len(wins) / len(trader_results) * 100
            text += f"\n*Résultat moyen* : `{avg:+.1f}%` par trade | *Win rate* : `{win_rate:.0f}%`\n"

        keyboard = []
        if len(recent_buys) >= 3:
            keyboard.append([InlineKeyboardButton("➕ Ajouter en Copy Trading (trader)", callback_data=f"quickaddtrader_{wallet_address}")])
        keyboard.append([InlineKeyboardButton("← Back", callback_data="menu_main")])
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
        except Exception as e:
            log.warning(f"Erreur d'affichage Markdown pour les achats récents de {wallet_address}: {e}")
            plain_text = text.replace("*", "").replace("`", "").replace("_", "")
            await query.edit_message_text(plain_text, reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)

    async def analyze_wallet_detail_inline(self, query, wallet_address: str):
        """
        🔍 Détail complet d'un wallet — reconstruction on-chain token par
        token (créations ET achats), checklist copy trading, note en
        étoiles, score bot, potentiel ATH. EX-CONTENU de analyze_wallet_inline
        avant sa scission (19 août) — inchangé, juste déclenché à la
        demande maintenant (bouton "🔍 Voir le détail complet") plutôt
        qu'automatiquement à chaque analyse. Voir analyze_wallet_inline
        pour le résumé rapide qui précède celle-ci.
        """
        import wallet_history
        import backtest
        import gmgn_client

        await query.edit_message_text(f"🔍 Détail complet du wallet `{wallet_address[:12]}...`...", parse_mode="Markdown")

        if not config.HELIUS_API_KEY:
            await query.edit_message_text("❌ Aucune clé HELIUS_API_KEY configurée.")
            return

        text = f"🔍 *Détail complet — {wallet_address[:12]}...*\n\n"

        # ── Statut DEV (a-t-il créé des tokens ?) ────────────────────────
        created_tokens = await wallet_history.get_created_tokens(wallet_address, max_results=10)
        text += f"🛠️ *Statut développeur* : `{len(created_tokens)}` token(s) créé(s) (sur les 10 derniers scannés)\n"

        dev_results = []
        for i, ct in enumerate(created_tokens):
            if len(created_tokens) > 1:
                try:
                    await query.edit_message_text(
                        f"🔍 Détail complet du wallet `{wallet_address[:12]}...`\n\n"
                        f"🛠️ Analyse des créations : `{i + 1}/{len(created_tokens)}` en cours...",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
            detail = await backtest.get_detailed_trade_info(
                ct["token_mint"], purchase_block_time=ct.get("block_time"),
            )
            dev_results.append(detail)
            status_icon = "✅" if detail["hit_tp"] else ("❌" if detail["hit_sl"] else "➖")
            if detail.get("symbol") and detail.get("dexscreener_url"):
                name_display = f"[{detail['symbol']}]({detail['dexscreener_url']})"
            elif detail.get("symbol"):
                name_display = f"{detail['symbol']} — `{ct['token_mint'][:8]}...`"
            else:
                name_display = f"`{ct['token_mint'][:8]}...`"
            mc_display = f", MC entrée: ${detail['entry_market_cap_usd']:,.0f}" if detail.get("entry_market_cap_usd") else ""
            text += f"   {status_icon} {name_display} → `{detail['result_pct']:+.1f}%` ({detail['purchase_date']}{mc_display})\n"

        if len(dev_results) >= 3:
            avg_dev = sum(r["result_pct"] for r in dev_results) / len(dev_results)
            wins_dev = [r for r in dev_results if r["result_pct"] > 0]
            win_rate_dev = len(wins_dev) / len(dev_results) * 100
            text += f"   *Résultat moyen* : `{avg_dev:+.1f}%` par token | *Win rate* : `{win_rate_dev:.0f}%`\n"

        # ── Statut TRADER (a-t-il acheté des tokens ?) ───────────────────
        recent_buys = await wallet_history.get_recent_buys(wallet_address, max_results=10)
        text += f"\n📈 *Statut trader* : `{len(recent_buys)}` achat(s) Pump.fun trouvé(s) (sur les 10 derniers scannés)\n"
        trader_results = []
        if len(recent_buys) >= 1:
            for i, buy in enumerate(recent_buys):
                try:
                    await query.edit_message_text(
                        f"🔍 Détail complet du wallet `{wallet_address[:12]}...`\n\n"
                        f"📈 Analyse des achats : `{i + 1}/{len(recent_buys)}` en cours "
                        f"(scan on-chain — peut prendre jusqu'à 1-2 min par token)...",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

                detail = await backtest.get_detailed_trade_info(
                    buy["token_mint"], purchase_block_time=buy.get("block_time"), tp_pct=100.0, sl_pct=config.SL_PCT,
                    sol_spent=buy.get("sol_spent"), tokens_received=buy.get("tokens_received"),
                )

                max_mc_filter = config.DEFAULT_WALLET_SETTINGS.get("max_market_cap")
                entry_mc = detail.get("entry_market_cap_usd")
                excluded_by_filter = bool(max_mc_filter and entry_mc and entry_mc > max_mc_filter)

                if not excluded_by_filter:
                    trader_results.append(detail)
                status_icon = "🚫" if excluded_by_filter else ("✅" if detail["hit_tp"] else ("❌" if detail["hit_sl"] else "➖"))

                if detail.get("symbol") and detail.get("dexscreener_url"):
                    name_display = f"[{detail['symbol']}]({detail['dexscreener_url']})"
                elif detail.get("symbol"):
                    name_display = f"{detail['symbol']} — `{buy['token_mint'][:8]}...`"
                else:
                    name_display = f"`{buy['token_mint'][:8]}...`"

                mc_display = f", MC entrée: ${detail['entry_market_cap_usd']:,.0f}" if detail.get("entry_market_cap_usd") else ""
                filter_note = f" _(> filtre {max_mc_filter:,.0f}$, exclu)_" if excluded_by_filter else ""
                text += f"   {status_icon} {name_display} → `{detail['result_pct']:+.1f}%` ({detail['purchase_date']}{mc_display}){filter_note}\n"

        if len(trader_results) >= 3:
            avg = sum(r["result_pct"] for r in trader_results) / len(trader_results)
            wins = [r for r in trader_results if r["result_pct"] > 0]
            win_rate = len(wins) / len(trader_results) * 100
            text += f"\n   *Résultat moyen* : `{avg:+.1f}%` par trade | *Win rate* : `{win_rate:.0f}%`\n"
            text += f"   _Pour tous les détails (market cap, liquidité, volume...) : `python analyze_wallet_detailed.py --wallet {wallet_address}`_\n"

            try:
                import copytrade_checklist
                await query.edit_message_text(
                    f"🔍 Détail complet du wallet `{wallet_address[:12]}...`\n\n📋 Checklist copy trading en cours (D, E, F, G)...",
                    parse_mode="Markdown",
                )
                freq = await copytrade_checklist.check_buy_frequency(wallet_address)
                balance = await copytrade_checklist.check_continuous_balance(wallet_address)
                win_7d = await copytrade_checklist.get_win_rate_7d(wallet_address)
                consistency = await copytrade_checklist.check_entry_consistency(wallet_address)

                text += "\n📋 *Checklist copy trading (D, E, F, G)*\n"
                text += f"   D — Fréquence : {freq['reason']}\n"
                text += f"   E — Wallet établi : {balance['reason']}\n"
                if win_7d.get("win_rate") is not None:
                    text += f"   F — Win rate 7j : `{win_7d['win_rate']:.0f}%` ({win_7d['reason']})\n"
                else:
                    text += f"   F — Win rate 7j : {win_7d['reason']}\n"
                text += f"   G — Cohérence d'entrée : {consistency['reason']}\n"
                text += "   _B, C1, C2 nécessitent un token de référence précis — non inclus ici._\n"
            except Exception as e:
                log.warning(f"Erreur checklist copy trading pour {wallet_address}: {e}")
                text += "\n📋 _Checklist copy trading indisponible (erreur pendant le calcul)._\n"

        all_results = dev_results + trader_results
        rating = _compute_star_rating(all_results)
        if rating:
            stars_display = "⭐" * rating["stars"] + "☆" * (5 - rating["stars"])
            text += f"\n{stars_display} *Note globale* : `{rating['stars']}/5` (sur `{rating['trade_count']}` trade(s) connus, créations + achats)\n"
        else:
            text += f"\n☆☆☆☆☆ *Note globale* : _pas assez de trades connus (minimum 3) pour noter ce wallet_\n"

        bot_score = await _compute_bot_probability(recent_buys)
        if bot_score:
            text += (
                f"{bot_score['label']} — score `{bot_score['score']}/100`\n"
                f"   _Vitesse d'achat moyenne après création : {bot_score['avg_speed_s']:.0f}s "
                f"({bot_score['fast_buy_pct']:.0f}% des achats en moins de 30s, sur {bot_score['sample_size']} trade(s))_\n"
            )

        try:
            await query.edit_message_text(
                f"🔍 Détail complet du wallet `{wallet_address[:12]}...`\n\n"
                f"📐 Calcul du potentiel ATH sur les 12 derniers tokens uniques...",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        ath_potential = await _compute_ath_potential(wallet_address, max_tokens=12)
        if ath_potential:
            text += (
                f"\n📐 *Potentiel ATH* (si vendu au sommet à chaque fois) : "
                f"`{ath_potential['avg_gap_pct']:+.0f}%` en moyenne, sur les "
                f"`{ath_potential['sample_size']}` derniers premiers achats uniques\n"
                f"   _⚠️ Théorique — personne ne vend jamais pile au sommet. "
                f"Mesure le potentiel des tokens choisis, pas un résultat réaliste._\n"
            )

        gmgn_stats = await gmgn_client.get_wallet_stats(wallet_address)
        pnl_stat = gmgn_stats.get("pnl_stat") if gmgn_stats else None
        if pnl_stat and pnl_stat.get("token_num", 0) > 0:
            try:
                realized_profit = float(gmgn_stats.get("realized_profit", 0) or 0)
                realized_profit_pnl = float(gmgn_stats.get("realized_profit_pnl", 0) or 0)
            except (TypeError, ValueError):
                realized_profit, realized_profit_pnl = 0.0, 0.0
            winrate_pct = pnl_stat.get("winrate", 0) * 100
            common = gmgn_stats.get("common") or {}
            tags = common.get("tags") or []
            tags_display = f" — étiquettes GMGN : {', '.join(tags)}" if tags else ""
            text += (
                f"\n🌐 *Selon GMGN* (source externe, {pnl_stat['token_num']} token(s) connus)"
                f"{tags_display}\n"
                f"   Profit réalisé : `${realized_profit:,.2f}` (`{realized_profit_pnl:+.1f}%`) | "
                f"Win rate : `{winrate_pct:.0f}%`\n"
            )

        keyboard = []
        if len(created_tokens) >= 3:
            keyboard.append([InlineKeyboardButton("➕ Ajouter en Ruggeur (dev)", callback_data=f"quickadddev_{wallet_address}")])
        if len(recent_buys) >= 3:
            keyboard.append([InlineKeyboardButton("➕ Ajouter en Copy Trading (trader)", callback_data=f"quickaddtrader_{wallet_address}")])
        if not keyboard:
            keyboard.append([InlineKeyboardButton("➕ Ajouter en Ruggeur", callback_data=f"quickadddev_{wallet_address}")])
        keyboard.append([InlineKeyboardButton("← Back", callback_data="menu_main")])
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
        except Exception as e:
            log.warning(f"Erreur d'affichage Markdown pour le détail de {wallet_address}: {e}")
            plain_text = text.replace("*", "").replace("`", "").replace("_", "")
            await query.edit_message_text(plain_text, reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)

    async def analyze_dev_inline(self, update: Update, dev_address: str):
        """
        🕵️ Analyse de dev — AJOUTÉ suite à une demande explicite. Version
        dédiée et allégée du bloc "Statut développeur" déjà présent dans
        analyze_wallet_inline (financement + statut trader inclus), pour
        quelqu'un qui veut évaluer UNIQUEMENT le profil créateur d'une
        adresse, sans le bruit du reste. Deux différences volontaires par
        rapport à "Analyse de wallet" (choisies explicitement) :
          - jusqu'à 12 créations (au lieu de 10) — accessible directement
            depuis le menu principal, pas besoin de passer par la version
            combinée pour ce détail en plus.
          - statut Fresh Wallet affiché ici aussi (déjà utilisé dans
            "Analyse de wallet" et par le Protection Scanner) — un dev sur
            une adresse fraîche (jamais active avant sa première création)
            est un signal pertinent pour évaluer le risque, indépendamment
            de son historique de créations.
        Volontairement SANS le "Statut trader" ni le financement détaillé
        de "Analyse de wallet" — reste focalisé sur le profil créateur.
        """
        import fund_tracer
        import wallet_history
        import backtest

        msg = await update.message.reply_text(f"🕵️ Analyse du dev `{dev_address[:12]}...`...", parse_mode="Markdown")

        if not config.HELIUS_API_KEY:
            await msg.edit_text("❌ Aucune clé HELIUS_API_KEY configurée.")
            return

        is_fresh = await fund_tracer.is_fresh_wallet(dev_address)

        text = f"🕵️ *Analyse de dev*\n`{dev_address}`\n\n"
        text += f"🆕 Fresh wallet : {'✅ Oui (jamais actif avant)' if is_fresh else '❌ Non (déjà actif avant)'}\n\n"

        created_tokens = await wallet_history.get_created_tokens(dev_address, max_results=12)
        text += f"🛠️ *Créations* : `{len(created_tokens)}` token(s) trouvé(s) (sur les 12 dernières recherchées)\n"

        if not created_tokens:
            text += "   _Aucune création Pump.fun trouvée pour cette adresse — ce n'est peut-être pas un dev._\n"

        dev_results = []
        for i, ct in enumerate(created_tokens):
            if len(created_tokens) > 1:
                try:
                    await msg.edit_text(
                        f"🕵️ Analyse du dev `{dev_address[:12]}...`\n\n"
                        f"🛠️ Analyse des créations : `{i + 1}/{len(created_tokens)}` en cours...",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
            detail = await backtest.get_detailed_trade_info(
                ct["token_mint"], purchase_block_time=ct.get("block_time"),
            )
            dev_results.append(detail)
            status_icon = "✅" if detail["hit_tp"] else ("❌" if detail["hit_sl"] else "➖")
            if detail.get("symbol") and detail.get("dexscreener_url"):
                name_display = f"[{detail['symbol']}]({detail['dexscreener_url']})"
            elif detail.get("symbol"):
                name_display = f"{detail['symbol']} — `{ct['token_mint'][:8]}...`"
            else:
                name_display = f"`{ct['token_mint'][:8]}...`"
            mc_display = f", MC entrée: ${detail['entry_market_cap_usd']:,.0f}" if detail.get("entry_market_cap_usd") else ""
            text += f"   {status_icon} {name_display} → `{detail['result_pct']:+.1f}%` ({detail['purchase_date']}{mc_display})\n"

        if len(dev_results) >= 3:
            avg_dev = sum(r["result_pct"] for r in dev_results) / len(dev_results)
            wins_dev = [r for r in dev_results if r["result_pct"] > 0]
            win_rate_dev = len(wins_dev) / len(dev_results) * 100
            text += f"\n*Résultat moyen* : `{avg_dev:+.1f}%` par token | *Win rate* : `{win_rate_dev:.0f}%`\n"

        rating = _compute_star_rating(dev_results)
        if rating:
            stars_display = "⭐" * rating["stars"] + "☆" * (5 - rating["stars"])
            text += f"\n{stars_display} *Note* : `{rating['stars']}/5` (sur `{rating['trade_count']}` création(s))\n"

        # PAS de score "probabilité bot" ici (contrairement à analyze_wallet_inline) :
        # _compute_bot_probability mesure la vitesse d'ACHAT après création — pour
        # un DEV, le block_time d'une création EST le moment de la création
        # elle-même, donc cette vitesse serait toujours ≈0 pour tout le monde.
        # Signal dégénéré, pas transposable tel quel au profil créateur.

        # Bouton d'ajout dès qu'au moins UNE création est trouvée — contrairement
        # au seuil de 3 dans "Analyse de wallet" (qui doit départager plusieurs
        # profils possibles), ici la personne est déjà venue spécifiquement
        # pour évaluer un profil dev, le seuil bas reste pertinent.
        keyboard = []
        if created_tokens:
            keyboard.append([InlineKeyboardButton("➕ Ajouter en Ruggeur (dev)", callback_data=f"quickadddev_{dev_address}")])
        keyboard.append([InlineKeyboardButton("← Back", callback_data="menu_main")])

        try:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
        except Exception as e:
            log.warning(f"Erreur d'affichage Markdown pour l'analyse de dev {dev_address}: {e}")
            plain_text = text.replace("*", "").replace("`", "").replace("_", "")
            await msg.edit_text(plain_text, reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)

    async def check_ai_score_inline(self, update: Update, input_address: str):
        """📈 Score IA complet — avis IA direct sur un dev ou un token, sans l'ajouter au monitoring."""
        import find_dev
        import fund_tracer
        import wallet_history
        import backtest
        import ai_advisor

        msg = await update.message.reply_text(f"📈 Calcul du score IA pour `{input_address[:12]}...`...", parse_mode="Markdown")

        if not config.HELIUS_API_KEY:
            await msg.edit_text("❌ Aucune clé HELIUS_API_KEY configurée.")
            return

        dev_address = input_address
        if len(input_address) > 40 and input_address.endswith("pump"):
            creator_info = await find_dev.find_token_creator(input_address)
            if not creator_info:
                await msg.edit_text("❌ Impossible de trouver le créateur de ce token.")
                return
            dev_address = creator_info["dev_address"]

        trace = await fund_tracer.get_first_funder(dev_address)
        past_tokens = await wallet_history.get_created_tokens(dev_address)

        if len(past_tokens) < 3:
            await msg.edit_text(
                f"📈 *Score IA*\n\n⏭️ Historique insuffisant (`{len(past_tokens)}` token(s)) — "
                f"il faut au moins 3 tokens créés pour calculer un score IA fiable."
            )
            return

        result = await backtest.backtest_wallet(past_tokens)
        regularity = await wallet_history.check_sell_regularity(dev_address, past_tokens)
        scheme = trace.get("scheme", "inconnu")

        ai_verdict = await ai_advisor.get_ai_verdict(dev_address, result, regularity, scheme)
        if not ai_verdict:
            await msg.edit_text("❌ Avis IA désactivé (fonctionnalité coupée manuellement — voir ai_advisor.py).")
            return

        verdict_icon = "✅" if ai_verdict["verdict"] == "GOOD" else ("⚠️" if ai_verdict["verdict"] == "AVOID" else "➖")
        # AJOUTÉ suite à une demande explicite : cette fonction n'affichait
        # que le SCHÉMA de financement (ex: "exchange"), sans jamais montrer
        # le montant, l'adresse du financeur, ni le statut fresh wallet —
        # ces infos étaient pourtant déjà calculées via trace, juste jamais
        # affichées. Mise en cohérence avec analyze_coin_inline
        # ("Analyse du token") et analyze_wallet_inline ("Analyse de
        # wallet"), qui affichaient déjà tout ça.
        is_fresh = await fund_tracer.is_fresh_wallet(dev_address)
        funding_details = ""
        if trace.get("funder_address"):
            funding_details = (
                f"Financé par : `{trace['funder_address'][:12]}...`\n"
                f"Montant : `{_format_sol_amount(trace.get('amount_sol', 0))}` SOL\n"
                f"Fresh wallet : {'✅ Oui' if is_fresh else '❌ Non (déjà actif avant)'}\n"
            )

        text = (
            f"📈 *Score IA complet*\n`{dev_address}`\n\n"
            f"{verdict_icon} Verdict ({ai_verdict['provider']}) : *{ai_verdict['verdict']}*\n\n"
            f"_{ai_verdict['reasoning']}_\n\n"
            f"── Données utilisées ──\n"
            f"Ratio gain/perte : `{result['ratio']:.2f}`\n"
            f"Score de régularité : `{regularity['regularity_score']:.2f}`\n"
            f"Schéma de financement : `{scheme}`\n"
            f"{funding_details}"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="menu_main")]])
        # CORRIGÉ — même filet de sécurité qu'ailleurs (voir analyze_coin_inline
        # et check_security_inline) : évite un échec silencieux si un
        # caractère spécial casse le Markdown.
        try:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard, disable_web_page_preview=True)
        except Exception as e:
            log.warning(f"Erreur d'affichage Markdown pour le score IA de {dev_address}: {e}")
            plain_text = text.replace("*", "").replace("`", "").replace("_", "")
            await msg.edit_text(plain_text, reply_markup=keyboard, disable_web_page_preview=True)

    async def show_position_calculator(self, query):
        """💰 Calcul position — calculateur simple de taille de position."""
        lang = self.data_store.state.get("language", "fr")
        text = t("poscalc_title", lang)
        keyboard = [[InlineKeyboardButton(t("btn_back", lang), callback_data="menu_main")]]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def compute_position_size(self, update: Update, budget: float, risk_pct: float):
        lang = self.data_store.state.get("language", "fr")
        suggested = budget * (risk_pct / 100)
        streak = int(100 / risk_pct) if risk_pct > 0 else "∞"
        text = t("poscalc_result", lang, budget=budget, risk_pct=risk_pct, suggested=suggested, streak=streak)
        await update.message.reply_text(text, parse_mode="Markdown")

    async def show_strategies_overview(self, query):
        """📜 Stratégies — page d'explication des stratégies avancées disponibles."""
        lang = self.data_store.state.get("language", "fr")
        text = t("strategies_overview", lang)
        keyboard = [[InlineKeyboardButton(t("btn_back", lang), callback_data="menu_main")]]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_session_stats(self, query):
        """📊 Stats session — même contenu que /stats, accessible depuis le menu."""
        lang = self.data_store.state.get("language", "fr")
        state = self.data_store.state
        closed = state.get("closed_positions", [])
        wins = [p for p in closed if p.get("total_sol_received", 0) >= p.get("total_sol_invested", 0)]
        n_ruggers, n_copytrade = self.data_store.count_wallets_by_mode()
        text = t(
            "session_stats", lang,
            pnl=state.get("total_pnl_usd", 0),
            n_ruggers=n_ruggers, n_copytrade=n_copytrade,
            total=self.data_store.count_wallets(), max_wallets=config.MAX_MONITORED_WALLETS,
            open_count=len(state.get("open_positions", [])), closed_count=len(closed),
            win_rate=(len(wins) / len(closed) * 100) if closed else 0,
        )
        keyboard = [[InlineKeyboardButton(t("btn_back", lang), callback_data="menu_main")]]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_global_alerts_toggle(self, query):
        """🔔 Alertes ON/OFF — coupe/active toutes les notifications Telegram d'un coup."""
        lang = self.data_store.state.get("language", "fr")
        current = self.data_store.state.get("global_alerts_enabled", True)
        status = "🟢 ON" if current else "🔴 OFF"
        text = t("global_alerts_title", lang, status=status)
        keyboard = [
            [InlineKeyboardButton(t("btn_toggle_status", lang, status=status), callback_data="togglealertsglobal")],
            [InlineKeyboardButton(t("btn_back", lang), callback_data="menu_main")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_global_stop(self, query):
        """🛑 Stop — coupe-circuit d'urgence : désactive tous les achats automatiques d'un coup."""
        current = self.data_store.state.get("global_auto_buy", True)
        status = "🟢 Achats actifs" if current else "🔴 TOUT ARRÊTÉ"
        text = (
            f"🛑 *Arrêt d'urgence*\n\nStatut actuel : {status}\n\n"
            f"Désactive instantanément l'achat automatique sur **tous** les wallets, "
            f"peu importe leurs réglages individuels. Les positions déjà ouvertes ne sont pas affectées."
        )
        keyboard = [
            [InlineKeyboardButton("🛑 TOUT ARRÊTER" if current else "🟢 Réactiver les achats", callback_data="toggleglobalstop")],
            [InlineKeyboardButton("← Back", callback_data="menu_main")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_more_menu(self, query):
        text = (
            "••• *Plus*\n\n"
            "Liens, réglages et fonctionnalités supplémentaires.\n\n"
            "💡 *Quick Buy by CA* : colle directement une adresse de token Solana "
            "dans le chat pour ouvrir une position manuelle en paper trading."
        )
        lang = self.data_store.state.get("language", "fr")
        lang_button_key = "language_button_fr" if lang == "fr" else "language_button_en"
        keyboard = [
            [
                InlineKeyboardButton(t(lang_button_key, lang), callback_data="togglelanguage"),
                InlineKeyboardButton("🎁 Parrainages", callback_data="referral"),
            ],
            [InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings")],
            [InlineKeyboardButton("📋 Presets", callback_data="list_presets")],
            [InlineKeyboardButton("💾 Backups", callback_data="menu_backups")],
            [InlineKeyboardButton("← Back", callback_data="menu_main")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_backups_menu(self, query):
        """Statut des backups automatiques + backup manuel à la demande."""
        backups = self.data_store.list_backups()
        text = (
            f"💾 *Backups*\n\n"
            f"_Sauvegarde automatique toutes les 5 minutes + au démarrage du bot "
            f"(rotation : {30} dernières conservées)._\n\n"
            f"Nombre de backups disponibles : `{len(backups)}`\n"
        )
        if backups:
            latest = os.path.basename(backups[0])
            text += f"Dernier backup : `{latest}`\n"
        text += (
            f"\n⚠️ La restauration se fait manuellement en copiant un fichier "
            f"depuis le dossier `backups/` par-dessus `sniper_data.json` — "
            f"pas de bouton de restauration ici, par sécurité (éviter d'écraser "
            f"ton état actuel par erreur d'un clic)."
        )
        keyboard = [
            [InlineKeyboardButton("💾 Backup maintenant", callback_data="backupnow")],
            [InlineKeyboardButton("← Back", callback_data="menu_more")],
        ]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_wallets_menu(self, query):
        """
        Page "Portefeuilles" — gestion multi-wallet réelle (Créer & Gérer ses
        Wallets). Les wallets créés/importés ici sont stockés CHIFFRÉS
        (voir wallet_manager.py). Le wallet actif est celui utilisé pour
        l'exécution LIVE.
        """
        lang = self.data_store.state.get("language", "fr")
        if not config.WALLET_ENCRYPTION_KEY:
            text = f"{t('wallets_title', lang)}\n\n{t('wallets_config_required', lang)}"
            keyboard = [
                [InlineKeyboardButton(t("btn_refresh", lang), callback_data="menu_wallets")],
                [InlineKeyboardButton(t("btn_back", lang), callback_data="menu_main")],
            ]
            await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)
            return

        from wallet_manager import WalletManager
        wm = WalletManager(self.data_store)
        wallets = wm.list_wallets()
        active_label = wm.get_active_label()

        lines = [f"{t('wallets_managed_title', lang, count=len(wallets))}\n"]
        keyboard = []

        for label, pubkey in wallets.items():
            marker = t("active_marker", lang) if label == active_label else ""
            try:
                balance = await wm.get_balance(label)
                lines.append(f"*{label}* {marker}\n`{pubkey}`\n{balance:.4f} SOL\n")
            except Exception:
                lines.append(f"*{label}* {marker}\n`{pubkey}`\n{t('balance_unavailable', lang)}\n")
            row = [InlineKeyboardButton(f"⚙️ {label}", callback_data=f"walletcfg_{label}")]
            if label != active_label:
                row.append(InlineKeyboardButton(t("btn_activate", lang), callback_data=f"setactivewallet_{label}"))
            keyboard.append(row)

        if not wallets:
            lines.append(t("no_wallets_managed", lang))

        if config.TRADING_MODE != "LIVE":
            lines.append(f"\n{t('paper_mode_note', lang)}")

        keyboard.append([
            InlineKeyboardButton(t("btn_generate", lang), callback_data="genwallet"),
            InlineKeyboardButton(t("btn_import", lang), callback_data="importwallet"),
        ])
        keyboard.append([InlineKeyboardButton(t("btn_refresh", lang), callback_data="menu_wallets")])
        keyboard.append([InlineKeyboardButton(t("btn_back", lang), callback_data="menu_main")])

        await self._send_or_edit(query, "\n".join(lines), InlineKeyboardMarkup(keyboard), edit=True)

    async def show_confirm(self, query, description: str, confirm_callback: str, cancel_callback: str):
        """
        Écran de confirmation générique pour toute action destructive/irréversible.
        description : phrase claire de ce qui va se passer si on confirme.
        confirm_callback : callback_data déclenché par ✅ Confirmer (do* le plus souvent).
        cancel_callback : callback_data déclenché par ❌ Annuler (retour au bon menu).
        """
        lang = self.data_store.state.get("language", "fr")
        text = t("confirm_title", lang, description=description)
        keyboard = [[
            InlineKeyboardButton(t("btn_confirm", lang), callback_data=confirm_callback),
            InlineKeyboardButton(t("btn_cancel", lang), callback_data=cancel_callback),
        ]]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_wallet_config(self, query, label: str):
        lang = self.data_store.state.get("language", "fr")
        from wallet_manager import WalletManager
        wm = WalletManager(self.data_store)
        wallets = wm.list_wallets()
        pubkey = wallets.get(label)
        if not pubkey:
            await query.edit_message_text(t("wallet_not_found", lang))
            return

        try:
            balance = await wm.get_balance(label)
            balance_line = f"{balance:.4f} SOL"
        except Exception:
            balance_line = t("balance_line_unavailable", lang)

        active = wm.get_active_label() == label
        status = t("wallet_status_active", lang) if active else t("wallet_status_inactive", lang)
        text = t("wallet_config_title", lang, label=label, pubkey=pubkey, balance=balance_line, status=status)
        keyboard = []
        if not active:
            keyboard.append([InlineKeyboardButton(t("btn_activate_wallet", lang), callback_data=f"setactivewallet_{label}")])
        keyboard.append([InlineKeyboardButton(t("btn_disperse_sol", lang), callback_data=f"dispersesol_{label}")])
        keyboard.append([InlineKeyboardButton(t("btn_delete_wallet", lang), callback_data=f"askdeletewallet_{label}")])
        keyboard.append([InlineKeyboardButton(t("btn_back", lang), callback_data="menu_wallets")])
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    async def show_referral(self, query, chat_id: int):
        lang = self.data_store.state.get("language", "fr")
        referral = self.data_store.get_referral_state()
        if not referral.get("code"):
            self.data_store.generate_referral_code(chat_id)
            referral = self.data_store.get_referral_state()

        text = t(
            "referral_title", lang,
            code=referral["code"], referred_count=referral["referred_count"],
            commission_pct=referral["commission_pct"],
        )
        keyboard = [[InlineKeyboardButton(t("btn_back", lang), callback_data="menu_more")]]
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    # ══════════════════════════════════════════════════════════
    # PRESETS
    # ══════════════════════════════════════════════════════════

    async def show_presets(self, query):
        lang = self.data_store.state.get("language", "fr")
        presets = self.data_store.state.get("presets", {})
        text = t("presets_title", lang)
        keyboard = []
        for name in presets:
            keyboard.append([
                InlineKeyboardButton(f"🗑 {name}", callback_data=f"askdelpreset_{name}"),
            ])
        if not presets:
            text += t("presets_empty", lang)
        keyboard.append([InlineKeyboardButton(t("btn_back", lang), callback_data="menu_ruggers")])
        await self._send_or_edit(query, text, InlineKeyboardMarkup(keyboard), edit=True)

    # ══════════════════════════════════════════════════════════
    # CALLBACK ROUTER
    # ══════════════════════════════════════════════════════════

    def _sid(self, address: str) -> str:
        """Raccourci pratique pour data_store.get_short_id, utilisé dans
        les callback_data pour respecter la limite de 64 caractères de
        Telegram (voir get_short_id pour le contexte complet)."""
        return self.data_store.get_short_id(address)

    def _expand_short_ids(self, data: str) -> str:
        """
        Reconstruit un callback_data en remplaçant chaque identifiant court
        ("sidXXXXXXXX", 11 caractères) par l'adresse complète qu'il
        représente — appelé une seule fois, au tout début du traitement
        d'un clic. Grâce à ça, les 51+ endroits du code qui analysent le
        callback_data (data.split(...), data[len(...):], etc.) continuent
        de fonctionner SANS AUCUNE MODIFICATION : ils voient l'adresse
        complète, exactement comme avant ce correctif.

        Le motif "sid" + 8 caractères hexadécimaux est délibérément
        distinctif (le préfixe littéral "sid" n'apparaît dans aucune autre
        partie des callback_data existants) pour éviter tout faux positif.
        """
        return re.sub(
            r"sid[0-9a-f]{8}(?![0-9a-f])",
            lambda m: self.data_store.resolve_short_id(m.group(0)),
            data,
        )

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data
        # Reconstruit l'adresse complète à partir de l'ID court AVANT tout
        # le reste du traitement — voir _expand_short_ids pour le contexte
        # complet (limite de 64 caractères des callback_data Telegram).
        data = self._expand_short_ids(data)
        chat_id = update.effective_chat.id

        if data == "menu_main":
            await self.show_main_menu(query, edit=True)
        elif data == "menu_ruggers":
            await self.show_ruggers_menu(query)
        elif data.startswith("ruggers_page_"):
            await self.show_ruggers_menu(query, page=int(data.split("_")[-1]))
        elif data == "menu_copytrade":
            await self.show_copytrade_menu(query)
        elif data.startswith("copytrade_page_"):
            await self.show_copytrade_menu(query, page=int(data.split("_")[-1]))
        elif data == "add_copytrade":
            user_states[chat_id] = {"awaiting": "add_copytrade_address"}
            await query.edit_message_text(
                "Colle l'adresse du wallet à suivre en copy trading.\n"
                "_Il sera ajouté en mode Track Buy par défaut — tu pourras "
                "changer pour Track Sell depuis sa page de config._"
            )
        elif data == "menu_settings":
            await self.show_settings_menu(query)
        elif data == "menu_wallets":
            await self.show_wallets_menu(query)
        elif data == "genwallet":
            user_states[chat_id] = {"awaiting": "gen_wallet_label"}
            await query.edit_message_text("Donne un nom à ce nouveau wallet (ex: `principal`, `test`).", parse_mode="Markdown")
        elif data == "importwallet":
            user_states[chat_id] = {"awaiting": "import_wallet_label"}
            await query.edit_message_text("Donne un nom à ce wallet à importer.")
        elif data.startswith("walletcfg_"):
            label = data[len("walletcfg_"):]
            await self.show_wallet_config(query, label)
        elif data.startswith("setactivewallet_"):
            label = data[len("setactivewallet_"):]
            from wallet_manager import WalletManager
            wm = WalletManager(self.data_store)
            try:
                wm.set_active_wallet(label)
                await query.answer(f"Wallet actif : {label}")
            except ValueError as e:
                await query.answer(str(e), show_alert=True)
            await self.show_wallet_config(query, label)
        elif data.startswith("askdeletewallet_"):
            label = data[len("askdeletewallet_"):]
            await self.show_confirm(
                query,
                f"supprimer le wallet `{label}` du bot (le wallet on-chain existe toujours, "
                f"juste retiré d'ici — sauvegarde sa clé privée avant si tu ne l'as pas déjà).",
                confirm_callback=f"dodeletewallet_{label}",
                cancel_callback=f"walletcfg_{label}",
            )
        elif data.startswith("dodeletewallet_"):
            label = data[len("dodeletewallet_"):]
            from wallet_manager import WalletManager
            wm = WalletManager(self.data_store)
            wm.remove_wallet(label)
            await query.answer(f"Wallet '{label}' supprimé du bot.", show_alert=True)
            await self.show_wallets_menu(query)
        elif data.startswith("dispersesol_"):
            label = data[len("dispersesol_"):]
            user_states[chat_id] = {"awaiting": "disperse_destination", "wallet_label": label}
            await query.edit_message_text(
                "Colle l'adresse Solana de destination pour l'envoi de SOL depuis ce wallet."
            )
        elif data.startswith("confirmdisperse_"):
            payload = data[len("confirmdisperse_"):]
            wallet_label, destination, amount_str = payload.split("|")
            await self._execute_disperse(query, wallet_label, destination, float(amount_str))
        elif data == "togglelanguage":
            current = self.data_store.state.get("language", "fr")
            self.data_store.state["language"] = "en" if current == "fr" else "fr"
            self.data_store.save()
            await self.show_main_menu(query, edit=True)
        elif data == "menu_positions":
            await self.show_positions(query)
        elif data == "menu_more":
            await self.show_more_menu(query)
        elif data == "analyzecoin":
            user_states[chat_id] = {"awaiting": "analyze_coin_address"}
            await query.edit_message_text("Colle l'adresse du token à analyser.")
        elif data == "analyzewallet":
            user_states[chat_id] = {"awaiting": "analyze_wallet_address"}
            await query.edit_message_text("Colle l'adresse du WALLET à analyser (financement, statut dev, statut trader).")
        elif data == "analyzedev":
            user_states[chat_id] = {"awaiting": "analyze_dev_address"}
            await query.edit_message_text("Colle l'adresse du DEV à analyser (jusqu'à 12 dernières créations + statut fresh wallet).")
        elif data.startswith("walletdetail_"):
            # AJOUTÉ (demande explicite, 19 août) : déclenche la reconstruction
            # on-chain complète (ex-comportement automatique de "Analyse de
            # wallet") — maintenant à la demande seulement, depuis le résumé
            # rapide GMGN. Voir analyze_wallet_detail_inline.
            address = data[len("walletdetail_"):]
            await self.analyze_wallet_detail_inline(query, address)
        elif data.startswith("walletbuys_"):
            # AJOUTÉ (demande explicite) : juste les 10 derniers achats avec
            # résultat par token — sans checklist/étoiles/score bot/potentiel
            # ATH. Voir analyze_wallet_buys_only_inline.
            address = data[len("walletbuys_"):]
            await self.analyze_wallet_buys_only_inline(query, address)
        elif data == "checksecurity":
            user_states[chat_id] = {"awaiting": "check_security_address"}
            await query.edit_message_text("Colle l'adresse du token à vérifier (sécurité/scam).")
        elif data == "checkaiscore":
            user_states[chat_id] = {"awaiting": "check_ai_score_address"}
            await query.edit_message_text("Colle l'adresse du dev ou du token pour calculer le score IA.")
        elif data == "menu_poscalc":
            await self.show_position_calculator(query)
            user_states[chat_id] = {"awaiting": "position_calc_input"}
        elif data == "menu_strategies":
            await self.show_strategies_overview(query)
        elif data == "menu_sessionstats":
            await self.show_session_stats(query)
        elif data == "menu_alertstoggle":
            await self.show_global_alerts_toggle(query)
        elif data == "togglealertsglobal":
            current = self.data_store.state.get("global_alerts_enabled", True)
            self.data_store.state["global_alerts_enabled"] = not current
            self.data_store.save()
            await self.show_global_alerts_toggle(query)
        elif data == "menu_globalstop":
            await self.show_global_stop(query)
        elif data == "toggleglobalstop":
            current = self.data_store.state.get("global_auto_buy", True)
            self.data_store.state["global_auto_buy"] = not current
            self.data_store.save()
            await self.show_global_stop(query)
        elif data == "menu_journal":
            await self.show_pnl_journal(query)
        elif data.startswith("journal_page_"):
            await self.show_pnl_journal(query, page=int(data.split("_")[-1]))
        elif data == "menu_help":
            await self.show_help(query)
        elif data.startswith("quickadddev_"):
            address = data[len("quickadddev_"):]
            if self.data_store.is_dev_monitored(address):
                await query.answer("Déjà dans le monitoring.", show_alert=True)
            elif not self.data_store.has_free_slot():
                await query.answer(f"Limite de {config.MAX_MONITORED_WALLETS} wallets atteinte.", show_alert=True)
            else:
                self.data_store.add_dev_wallet(address, label=address[:8] + "...", scheme="manuel", backtest_ratio=0.0)
                await query.answer("✅ Rugger ajouté !", show_alert=True)
        elif data.startswith("quickaddtrader_"):
            # AJOUTÉ suite à un bug signalé : "quickadddev_" ajoutait toujours
            # en mode track_creation, même pour un wallet identifié comme
            # TRADER (achats rentables) plutôt que DEV (créateur de tokens).
            # Ce handler ajoute en mode track_buy — le vrai mode copy trading
            # — et fait apparaître le wallet dans le menu 📋 Copy Trading
            # plutôt que 👤 Ruggers (voir la logique de filtrage par mode
            # ligne ~270 et ~321 plus haut dans ce fichier).
            address = data[len("quickaddtrader_"):]
            if self.data_store.is_dev_monitored(address):
                await query.answer("Déjà dans le monitoring.", show_alert=True)
            elif not self.data_store.has_free_slot():
                await query.answer(f"Limite de {config.MAX_MONITORED_WALLETS} wallets atteinte.", show_alert=True)
            else:
                self.data_store.add_dev_wallet(
                    address, label=address[:8] + "...", scheme="manuel", backtest_ratio=0.0, mode="track_buy"
                )
                await query.answer("✅ Wallet ajouté en Copy Trading !", show_alert=True)
        elif data.startswith("quickaddcluster_"):
            # AJOUTÉ avec la recherche de cluster (méthode adresse
            # intermédiaire d'exchange, voir analyze_coin_inline) — ajoute
            # TOUTES les adresses du cluster (l'adresse de départ + toutes
            # les ⭐ trouvées) en une fois, plutôt qu'une par une. Lit la
            # liste depuis le cache rempli au moment de l'analyse (évite de
            # relancer toute la recherche, potentiellement plusieurs minutes,
            # juste pour ce clic).
            dev_address = data[len("quickaddcluster_"):]
            cluster = self.data_store.state.get("cluster_cache", {}).get(dev_address, [])
            if not cluster:
                await query.answer("Cluster introuvable (relance l'analyse du token).", show_alert=True)
            else:
                added, skipped = 0, 0
                for addr in cluster:
                    if self.data_store.is_dev_monitored(addr):
                        skipped += 1
                        continue
                    if not self.data_store.has_free_slot():
                        break
                    self.data_store.add_dev_wallet(addr, label=addr[:8] + "...", scheme="cluster", backtest_ratio=0.0)
                    added += 1
                await query.answer(f"✅ {added} adresse(s) ajoutée(s) ({skipped} déjà présente(s)).", show_alert=True)
        elif data.startswith("markintermediate_"):
            # AJOUTÉ (demande explicite, 19 août) : complète le flux commencé
            # au moment d'ajouter un rugger manuellement — demande
            # maintenant l'intervalle de montant à surveiller sur cette
            # adresse. Voir monitoring_list.add_protection_target
            # (paramètre "ranges") pour la suite.
            address = data[len("markintermediate_"):]
            user_states[chat_id] = {"awaiting": "add_intermediate_range", "address": address}
            await query.edit_message_text(
                f"🔗 Wallet intermédiaire : `{address[:8]}...`\n\n"
                "Envoie le montant MIN et MAX (en SOL) à surveiller, séparés par un espace.\n\n"
                "Exemple : `1.5 2.5` → tout wallet ayant reçu entre 1.5 et 2.5 SOL depuis "
                "cette adresse sera automatiquement ajouté au monitoring.",
                parse_mode="Markdown",
            )
        elif data.startswith("quickaddprotection_"):
            # AJOUTÉ suite à une question explicite : le scanner de
            # protection (protection_scanner.py) tourne en tâche de fond
            # mais surveillait une liste TOUJOURS VIDE — rien n'appelait
            # jamais add_protection_target() nulle part dans le bot
            # Telegram. Ce handler ferme cette boucle : enregistre
            # l'adresse exchange trouvée pendant l'analyse comme cible
            # surveillée EN PERMANENCE — le scanner détectera désormais
            # automatiquement tout NOUVEAU wallet financé par le même
            # montant depuis cette même adresse, sans avoir à relancer une
            # analyse manuelle à chaque fois.
            funder_address = data[len("quickaddprotection_"):]
            cached = self.data_store.state.get("protection_target_cache", {}).get(funder_address)
            already_watched = any(
                t.get("address") == funder_address for t in self.data_store.state.get("protection_targets", {}).values()
            )
            if not cached:
                await query.answer("Infos introuvables (relance l'analyse du token).", show_alert=True)
            elif already_watched:
                await query.answer("Déjà surveillé.", show_alert=True)
            else:
                self.data_store.add_protection_target(
                    label=cached["label"],
                    target_type="exchange",
                    address=funder_address,
                    amount_sol=cached.get("amount_sol"),
                )
                await query.answer("🛡️ Exchange ajouté à la surveillance permanente !", show_alert=True)
        elif data == "gas_fees":
            await self.show_gas_fees(query)
        elif data == "notif_settings":
            await self.show_notification_settings(query)
        elif data.startswith("togglenotif_"):
            key = data.split("_", 1)[1]
            self.data_store.toggle_notification_pref(key)
            await self.show_notification_settings(query)
        elif data == "toggle_global_auto_buy":
            self.data_store.toggle_global_auto_buy()
            await self.show_ruggers_menu(query)
        elif data == "toggle_global_auto_sell":
            self.data_store.toggle_global_auto_sell()
            await self.show_ruggers_menu(query)
        elif data == "toggleglobalai":
            self.data_store.toggle_global_ai()
            await self.show_settings_menu(query)
        elif data == "cleanup_settings":
            await self.show_cleanup_settings(query)
        elif data == "cleanup_toggle":
            current = self.data_store.get_cleanup_settings()["enabled"]
            self.data_store.set_cleanup_settings(enabled=not current)
            await self.show_cleanup_settings(query)
        elif data == "cleanup_edit_inactive_days":
            user_states[chat_id] = {"awaiting": "cleanup_inactive_days"}
            await query.edit_message_text("Envoie le nombre de jours d'inactivité max avant suppression (ex: 30).")
        elif data == "cleanup_edit_max_losses":
            user_states[chat_id] = {"awaiting": "cleanup_max_losses"}
            await query.edit_message_text("Envoie le nombre de pertes consécutives max avant suppression (ex: 7).")
        elif data == "withdrawal_settings":
            await self.show_withdrawal_settings(query)
        elif data == "withdrawal_toggle":
            current = self.data_store.get_withdrawal_alert_settings()["enabled"]
            self.data_store.set_withdrawal_alert_settings(enabled=not current)
            await self.show_withdrawal_settings(query)
        elif data == "withdrawal_edit_min_sol":
            user_states[chat_id] = {"awaiting": "withdrawal_min_sol"}
            await query.edit_message_text("Envoie le montant minimum en SOL qui déclenche l'alerte (ex: 0.1).")
        elif data == "withdrawal_edit_min_usdc":
            user_states[chat_id] = {"awaiting": "withdrawal_min_usdc"}
            await query.edit_message_text("Envoie le montant minimum en USDC qui déclenche l'alerte (ex: 20).")
        elif data == "withdrawal_toggle_autoadd":
            current = self.data_store.get_withdrawal_alert_settings().get("auto_add", True)
            self.data_store.set_withdrawal_alert_settings(auto_add=not current)
            await self.show_withdrawal_settings(query)
        elif data == "withdrawal_toggle_cascade":
            current = self.data_store.get_withdrawal_alert_settings().get("allow_cascade", False)
            self.data_store.set_withdrawal_alert_settings(allow_cascade=not current)
            await self.show_withdrawal_settings(query)
        elif data == "devtransfer_settings":
            await self.show_dev_transfer_settings(query)
        elif data == "devtransfer_edit_pct":
            user_states[chat_id] = {"awaiting": "devtransfer_pct"}
            await query.edit_message_text("Envoie le seuil en % du solde qui déclenche l'alerte (ex: 90).")
        elif data == "devtransfer_toggle_autoadd":
            current = self.data_store.get_dev_transfer_settings().get("auto_add", True)
            self.data_store.set_dev_transfer_settings(auto_add=not current)
            await self.show_dev_transfer_settings(query)
        elif data == "devtransfer_toggle_cascade":
            current = self.data_store.get_dev_transfer_settings().get("allow_cascade", False)
            self.data_store.set_dev_transfer_settings(allow_cascade=not current)
            await self.show_dev_transfer_settings(query)
        elif data == "add_rugger":
            user_states[chat_id] = {"awaiting": "add_rugger_address"}
            await query.edit_message_text("Colle l'adresse du wallet à ajouter au monitoring.")
        elif data == "list_presets":
            await self.show_presets(query)
        elif data == "menu_backups":
            await self.show_backups_menu(query)
        elif data == "backupnow":
            self.data_store._create_backup(reason="manual")
            await query.answer("💾 Backup créé.")
            await self.show_backups_menu(query)
        elif data.startswith("askdelpreset_"):
            name = data[len("askdelpreset_"):]
            await self.show_confirm(
                query,
                f"supprimer le preset *'{name}'*.",
                confirm_callback=f"dodelpreset_{name}",
                cancel_callback="list_presets",
            )
        elif data.startswith("dodelpreset_"):
            name = data[len("dodelpreset_"):]
            self.data_store.delete_preset(name)
            await self.show_presets(query)
        elif data.startswith("rugger_"):
            address = data.split("_", 1)[1]
            await self.show_rugger_config(query, address)
        elif data.startswith("ruggermore_"):
            address = data.split("_", 1)[1]
            await self.show_rugger_config_more(query, address)
        elif data.startswith("analyzethiswallet_"):
            # AJOUTÉ (demande explicite, 19 août) : lance "Analyse de wallet"
            # directement depuis la fiche d'un wallet Copy Trading, sans
            # retourner au menu principal ni recoller l'adresse.
            address = data[len("analyzethiswallet_"):]
            await self.analyze_wallet_inline(query, address)
        elif data.startswith("analyzethisdev_"):
            # Même principe, "Analyse de dev" pour un Ruggeur.
            address = data[len("analyzethisdev_"):]
            await self.analyze_dev_inline(query, address)
        elif data.startswith("toggle_ab_"):
            address = data.split("_", 2)[2]
            s = self.data_store.get_wallet_settings(address)
            self.data_store.update_wallet_settings(address, {"auto_buy": not s.get("auto_buy", True)})
            await self.show_rugger_config(query, address)
        elif data.startswith("toggle_as_"):
            address = data.split("_", 2)[2]
            s = self.data_store.get_wallet_settings(address)
            self.data_store.update_wallet_settings(address, {"auto_sell": not s.get("auto_sell", True)})
            await self.show_rugger_config(query, address)
        elif data.startswith("tracking_"):
            address = data.split("_", 1)[1]
            await self.show_tracking_mode(query, address)
        elif data.startswith("setmode_"):
            _, address, mode = data.split("_", 2)
            self.data_store.state["monitored_dev_wallets"][address]["mode"] = mode
            self.data_store.save()
            await self.show_tracking_mode(query, address)
        elif data.startswith("buyconfig_"):
            address = data.split("_", 1)[1]
            await self.show_buy_config(query, address)
        elif data.startswith("sellconfig_"):
            address = data.split("_", 1)[1]
            await self.show_sell_config(query, address)
        elif data.startswith("protection_"):
            address = data.split("_", 1)[1]
            await self.show_protection_config(query, address)
        elif data.startswith("securityai_"):
            address = data.split("_", 1)[1]
            await self.show_security_ai_config(query, address)
        elif data.startswith("snipeconfig_"):
            address = data.split("_", 1)[1]
            await self.show_snipe_config(query, address)
        elif data.startswith("protmode_"):
            address = data.split("_", 1)[1]
            await self.show_protection_mode(query, address)
        elif data.startswith("setprotmode_"):
            _, address, mode = data.split("_", 2)
            self.data_store.update_wallet_settings(address, {"protection_mode": mode})
            await self.show_protection_mode(query, address)
        elif data.startswith("maxloss_"):
            address = data.split("_", 1)[1]
            await self.show_max_loss_counter(query, address)
        elif data.startswith("unpause_"):
            address = data.split("_", 1)[1]
            self.data_store.unpause_wallet(address)
            await self.show_max_loss_counter(query, address)
        elif data.startswith("toggleset_"):
            _, address, field = data.split("_", 2)
            s = self.data_store.get_wallet_settings(address)
            self.data_store.update_wallet_settings(address, {field: not s.get(field, False)})
            # retourne à la page appropriée selon le champ
            if field in ("buy_only_once",):
                await self.show_buy_config(query, address)
            elif field in ("trailing_sl_enabled",):
                await self.show_sell_config(query, address)
            elif field in ("security_check_enabled", "ai_advisor_enabled"):
                await self.show_security_ai_config(query, address)
            elif field in ("anti_mev_enabled", "use_jito", "skip_price_check_for_speed"):
                await self.show_snipe_config(query, address)
            elif field in ("multibuy_enabled",):
                await self.show_multibuy_config(query, address)
            elif field in ("pullback_entry_enabled", "mc_trailing_enabled", "profit_trail_enabled"):
                await self.show_advanced_strategies(query, address)
            else:
                await self.show_protection_config(query, address)
        elif data.startswith("editset_"):
            # Format : editset_{address}|{field}|{value_type}[|none]
            payload = data[len("editset_"):]
            address, rest = payload.split("|", 1)
            meta_parts = rest.split("|")
            field = meta_parts[0]
            value_type = meta_parts[1]
            allow_none = len(meta_parts) > 2 and meta_parts[2] == "none"
            user_states[chat_id] = {
                "awaiting": "edit_setting", "address": address,
                "field": field, "value_type": value_type, "allow_none": allow_none,
            }
            none_hint = " (envoie `off` pour désactiver)" if allow_none else ""
            await query.edit_message_text(f"Envoie la nouvelle valeur pour `{field}`{none_hint}.", parse_mode="Markdown")
        elif data.startswith("edittp_"):
            address = data.split("_", 1)[1]
            user_states[chat_id] = {"awaiting": "edit_tp", "address": address}
            await query.edit_message_text(
                "Envoie les niveaux de TP au format `pct:ratio,pct:ratio` "
                "(ex: `50:0.5,150:1.0` = vendre 50% à +50%, le reste à +150%)."
            )
        elif data.startswith("transferranges_"):
            address = data.split("_", 1)[1]
            user_states[chat_id] = {"awaiting": "edit_transfer_ranges", "address": address}
            await query.edit_message_text(
                "Envoie jusqu'à 3 ranges au format `min:max,min:max` en SOL "
                "(ex: `1.48:1.50,5.66:5.68`)."
            )
        elif data.startswith("savepreset_"):
            address = data.split("_", 1)[1]
            user_states[chat_id] = {"awaiting": "save_preset_name", "address": address}
            await query.edit_message_text("Donne un nom à ce preset.")
        elif data.startswith("askreset_"):
            address = data[len("askreset_"):]
            await self.show_confirm(
                query,
                "remettre TOUS les réglages de ce rugger à leurs valeurs par défaut.",
                confirm_callback=f"doreset_{address}",
                cancel_callback=f"rugger_{address}",
            )
        elif data.startswith("doreset_"):
            address = data[len("doreset_"):]
            self.data_store.state["monitored_dev_wallets"][address]["settings"] = copy.deepcopy(config.DEFAULT_WALLET_SETTINGS)
            self.data_store.save()
            await self.show_rugger_config(query, address)
        elif data.startswith("rename_"):
            address = data.split("_", 1)[1]
            user_states[chat_id] = {"awaiting": "rename", "address": address}
            await query.edit_message_text("Envoie le nouveau nom pour ce rugger.")
        elif data.startswith("askdelete_"):
            address = data[len("askdelete_"):]
            entry = self.data_store.state["monitored_dev_wallets"].get(address, {})
            label = entry.get("label", address[:8] + "...")
            back_to = "menu_copytrade" if entry.get("mode") in ("track_buy", "track_sell") else "menu_ruggers"
            await self.show_confirm(
                query,
                f"supprimer définitivement `{label}` du monitoring.",
                confirm_callback=f"dodelete_{address}",
                cancel_callback=back_to,
            )
        elif data.startswith("dodelete_"):
            address = data[len("dodelete_"):]
            entry = self.data_store.state["monitored_dev_wallets"].get(address, {})
            back_to_copytrade = entry.get("mode") in ("track_buy", "track_sell")
            self.data_store.remove_dev_wallet(address)
            if back_to_copytrade:
                await self.show_copytrade_menu(query)
            else:
                await self.show_ruggers_menu(query)
        elif data.startswith("asksellall"):
            open_count = len(self.data_store.state.get("open_positions", []))
            if open_count == 0:
                await query.answer("Aucune position ouverte à vendre.", show_alert=True)
                return
            await self.show_confirm(
                query,
                f"vendre TOUTES les positions ouvertes ({open_count}), au marché actuel.",
                confirm_callback="dosellall",
                cancel_callback="menu_positions",
            )
        elif data.startswith("dosellall"):
            await self._sell_all(query)
        elif data.startswith("sell_"):
            _, idx, pct = data.split("_")
            await self._sell_position(query, int(idx), int(pct))
        elif data.startswith("qsell_"):
            # Format : qsell_{token_mint}_{pct} — vente rapide depuis l'alerte d'achat directe
            payload = data[len("qsell_"):]
            token_mint, pct_str = payload.rsplit("_", 1)
            await self._quick_sell_by_mint(query, token_mint, int(pct_str))
        elif data.startswith("refreshpnl_"):
            token_mint = data[len("refreshpnl_"):]
            await self._refresh_pnl(query, token_mint)
        elif data == "quickbuy_cancel":
            await query.edit_message_text("Achat annulé.")
        elif data == "quickbuy_custom":
            # CORRIGÉ : ce bloc et le précédent (quickbuy_cancel) étaient
            # placés APRÈS le check générique "quickbuy_" (prefix), qui les
            # interceptait toujours en premier dans cette chaîne if/elif —
            # rendant "Cancel" et "Custom" injoignables, provoqué un vrai
            # plantage (ValueError: not enough values to unpack) quand on
            # cliquait sur Cancel. Remontés avant le check générique.
            user_states[chat_id] = {"awaiting": "quick_buy_custom", "token": context.user_data.get("quick_buy_token")}
            await query.edit_message_text("Envoie le montant en SOL.")
        elif data.startswith("quickbuy_"):
            await self._handle_quick_buy_amount(query, data)
        # ── Buy Mode Simple/Hardcore ──────────────────────────────
        elif data.startswith("buymode_"):
            address = data.split("_", 1)[1]
            await self.show_buy_mode(query, address)
        elif data.startswith("confirmhardcore_"):
            address = data.split("_", 1)[1]
            await self.show_hardcore_confirmation(query, address)
        elif data.startswith("setbuymode_"):
            _, address, mode = data.split("_", 2)
            self.data_store.update_wallet_settings(address, {"buy_mode": mode, "hardcore_confirmed": mode == "hardcore"})
            await self.show_rugger_config(query, address)
        # ── Buy The Dip ────────────────────────────────────────────
        elif data.startswith("buythedip_"):
            address = data.split("_", 1)[1]
            await self.show_buy_the_dip(query, address)
        elif data.startswith("multibuy_"):
            address = data.split("_", 1)[1]
            await self.show_multibuy_config(query, address)
        elif data.startswith("editmultibuywallets_"):
            address = data[len("editmultibuywallets_"):]
            from wallet_manager import WalletManager
            wm = WalletManager(self.data_store)
            available = ", ".join(wm.list_wallets().keys()) or "aucun wallet géré"
            user_states[chat_id] = {"awaiting": "edit_multibuy_wallets", "address": address}
            await query.edit_message_text(
                f"Wallets disponibles : `{available}`\n\n"
                f"Envoie les labels à utiliser, séparés par des virgules (ex: `w1, w2, w3`).",
                parse_mode="Markdown",
            )
        elif data.startswith("editdip_"):
            address = data.split("_", 1)[1]
            user_states[chat_id] = {"awaiting": "edit_dip", "address": address}
            await query.edit_message_text(
                "Envoie jusqu'à 3 paliers au format `drop:amount,drop:amount` "
                "(ex: `30:0.5,50:1.0,70:2.0` = -30% ATH → +0.5 SOL, etc.)"
            )
        elif data.startswith("cleardip_"):
            address = data.split("_", 1)[1]
            self.data_store.update_wallet_settings(address, {"buy_the_dip_levels": []})
            await self.show_buy_the_dip(query, address)
        # ── Auto-Sell on Big Buy ───────────────────────────────────
        elif data.startswith("bigbuy_"):
            address = data.split("_", 1)[1]
            await self.show_big_buy_config(query, address)
        elif data.startswith("advstrategies_"):
            address = data[len("advstrategies_"):]
            await self.show_advanced_strategies(query, address)
        elif data.startswith("editbigbuy_"):
            address = data.split("_", 1)[1]
            user_states[chat_id] = {"awaiting": "edit_bigbuy", "address": address}
            await query.edit_message_text(
                "Envoie jusqu'à 3 paliers au format `min-max:ratio,min-max:ratio` en SOL "
                "(ex: `2.5-3:0.5,4-10:1.0` = 2.5-3 SOL → vendre 50%, 4-10 SOL → vendre 100%)"
            )
        elif data.startswith("clearbigbuy_"):
            address = data.split("_", 1)[1]
            self.data_store.update_wallet_settings(address, {"auto_sell_big_buy_levels": []})
            await self.show_big_buy_config(query, address)
        # ── Trailing SL multi-paliers ────────────────────────────────
        elif data.startswith("trailingtiers_"):
            address = data.split("_", 1)[1]
            await self.show_trailing_tiers(query, address)
        elif data.startswith("edittrailing_"):
            address = data.split("_", 1)[1]
            user_states[chat_id] = {"awaiting": "edit_trailing", "address": address}
            await query.edit_message_text(
                "Envoie jusqu'à 5 paliers au format `mc:trailing_pct,mc:trailing_pct` "
                "(ex: `0:20,100000:30` = par défaut -20%, au-dessus de 100k MC -30%)"
            )
        # ── Child Address Defaults ────────────────────────────────
        elif data.startswith("childdefaults_"):
            address = data.split("_", 1)[1]
            await self.show_child_defaults(query, address)
        elif data.startswith("togglechild_"):
            _, address, field = data.split("_", 2)
            s = self.data_store.get_wallet_settings(address)
            cd = s.get("child_defaults", {})
            cd[field] = not cd.get(field, False)
            self.data_store.update_wallet_settings(address, {"child_defaults": cd})
            await self.show_child_defaults(query, address)
        elif data.startswith("editchildh_"):
            address = data.split("_", 1)[1]
            user_states[chat_id] = {"awaiting": "edit_child_hours", "address": address}
            await query.edit_message_text("Envoie le nombre d'heures avant suppression (Remove if no launch).")
        # ── Sell Initials ──────────────────────────────────────────
        elif data.startswith("sellinitials_"):
            idx = int(data.split("_", 1)[1])
            await self._sell_initials_position(query, idx)
        # ── Referral ───────────────────────────────────────────────
        elif data == "referral":
            await self.show_referral(query, chat_id)

    # ══════════════════════════════════════════════════════════
    # SAISIE TEXTE (adresses, valeurs de settings, noms, quick buy)
    # ══════════════════════════════════════════════════════════

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        text = update.message.text.strip()
        state = user_states.get(chat_id)

        # Quick Buy by CA : toute adresse Solana envoyée sans état en cours
        if not state and _is_solana_address(text):
            await self._offer_quick_buy(update, context, text)
            return

        if not state:
            return

        awaiting = state["awaiting"]

        if awaiting == "add_rugger_address":
            if not _is_solana_address(text):
                await update.message.reply_text("Adresse invalide, réessaie.")
                return
            if _looks_like_token_mint(text):
                await update.message.reply_text(
                    "⚠️ Cette adresse ressemble à un TOKEN (se termine par 'pump'), pas à un wallet dev. "
                    "Utilise plutôt '🔍 Analyser un coin' pour obtenir l'adresse du VRAI dev à partir de ce token, "
                    "puis ajoute cette adresse-là en Ruggeur."
                )
                user_states.pop(chat_id, None)
                return
            if not self.data_store.has_free_slot():
                await update.message.reply_text(f"Limite de {config.MAX_MONITORED_WALLETS} wallets atteinte.")
                user_states.pop(chat_id, None)
                return
            self.data_store.add_dev_wallet(text, label=text[:8] + "...", scheme="manuel", backtest_ratio=0.0)
            # AJOUTÉ (demande explicite, 19 août) : propose de marquer le
            # wallet fraîchement ajouté comme "adresse intermédiaire" —
            # réutilise l'infrastructure Protection Scanner déjà existante
            # (Transfer Ranges) plutôt qu'un nouveau système séparé : le
            # wallet devient sa propre "cible" de protection
            # (parent_address = lui-même), avec les intervalles de montant
            # donnés ci-dessous stockés dans ses propres settings
            # (transfer_ranges) — voir protection_scanner._resolve_child_settings,
            # qui va déjà chercher exactement là.
            keyboard = [[
                InlineKeyboardButton("🔀 Oui, adresse intermédiaire", callback_data=f"markintermediate_{text}"),
                InlineKeyboardButton("Non", callback_data="menu_ruggers"),
            ]]
            await update.message.reply_text(
                f"✅ Rugger ajouté : `{text[:8]}...`\n\n"
                f"Marquer comme *adresse intermédiaire* ? Le bot surveillera alors en continu ses transferts "
                f"sortants et ajoutera automatiquement (en arrière-plan) toute adresse ayant reçu un montant "
                f"dans l'intervalle que tu donneras.",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard),
            )
            user_states.pop(chat_id, None)

        elif awaiting == "add_copytrade_address":
            if not _is_solana_address(text):
                await update.message.reply_text("Adresse invalide, réessaie.")
                return
            if _looks_like_token_mint(text):
                await update.message.reply_text(
                    "⚠️ Cette adresse ressemble à un TOKEN (se termine par 'pump'), pas à un wallet trader. "
                    "Utilise plutôt '🔍 Analyser un coin' pour trouver de vrais acheteurs de ce token, "
                    "puis ajoute LEUR adresse en Copy Trading."
                )
                user_states.pop(chat_id, None)
                return
            if not self.data_store.has_free_slot():
                await update.message.reply_text(f"Limite de {config.MAX_MONITORED_WALLETS} wallets atteinte.")
                user_states.pop(chat_id, None)
                return
            self.data_store.add_dev_wallet(
                text, label=text[:8] + "...", scheme="manuel", backtest_ratio=0.0, mode="track_buy",
            )
            await update.message.reply_text(
                f"✅ Wallet ajouté en Copy Trading (Track Buy) : `{text[:8]}...`", parse_mode="Markdown",
            )
            user_states.pop(chat_id, None)

        elif awaiting == "analyze_coin_address":
            if not _is_solana_address(text):
                await update.message.reply_text("Adresse invalide, réessaie.")
                return
            user_states.pop(chat_id, None)
            await self.analyze_coin_inline(update, text)

        elif awaiting == "analyze_wallet_address":
            if not _is_solana_address(text):
                await update.message.reply_text("Adresse invalide, réessaie.")
                return
            if _looks_like_token_mint(text):
                await update.message.reply_text(
                    "⚠️ Cette adresse ressemble à un TOKEN (se termine par 'pump'), pas à un wallet — "
                    "d'où le résultat vide que tu obtiendrais (0 créations, 0 achats). "
                    "Utilise plutôt '🔍 Analyser un coin' pour ce genre d'adresse."
                )
                user_states.pop(chat_id, None)
                return
            user_states.pop(chat_id, None)
            await self.analyze_wallet_inline(update, text)

        elif awaiting == "analyze_dev_address":
            if not _is_solana_address(text):
                await update.message.reply_text("Adresse invalide, réessaie.")
                return
            if _looks_like_token_mint(text):
                await update.message.reply_text(
                    "⚠️ Cette adresse ressemble à un TOKEN (se termine par 'pump'), pas à un wallet — "
                    "d'où le résultat vide que tu obtiendrais. "
                    "Utilise plutôt '🔍 Analyser un coin' pour trouver le VRAI dev à partir de ce token."
                )
                user_states.pop(chat_id, None)
                return
            user_states.pop(chat_id, None)
            await self.analyze_dev_inline(update, text)

        elif awaiting == "add_intermediate_range":
            address = state.get("address")
            parts = text.split()
            if len(parts) != 2:
                await update.message.reply_text("Format invalide. Envoie deux nombres séparés par un espace, ex: `1.5 2.5`.", parse_mode="Markdown")
                return
            try:
                min_sol, max_sol = float(parts[0]), float(parts[1])
            except ValueError:
                await update.message.reply_text("Montants invalides, réessaie (ex: `1.5 2.5`).", parse_mode="Markdown")
                return
            if min_sol < 0 or max_sol <= min_sol:
                await update.message.reply_text("Le max doit être supérieur au min, et les deux positifs. Réessaie.")
                return

            user_states.pop(chat_id, None)
            self.data_store.add_protection_target(
                label=f"intermediaire_{address[:8]}",
                target_type="exchange",
                address=address,
                ranges=[{"min": min_sol, "max": max_sol}],
            )
            await update.message.reply_text(
                f"✅ Wallet intermédiaire configuré : `{address[:8]}...`\n"
                f"Intervalle surveillé : `{min_sol}` – `{max_sol}` SOL\n\n"
                "Tout wallet recevant un montant dans cet intervalle depuis cette adresse "
                "sera automatiquement ajouté au monitoring.",
                parse_mode="Markdown",
            )

        elif awaiting == "check_security_address":
            if not _is_solana_address(text):
                await update.message.reply_text("Adresse invalide, réessaie.")
                return
            user_states.pop(chat_id, None)
            await self.check_security_inline(update, text)

        elif awaiting == "check_ai_score_address":
            if not _is_solana_address(text):
                await update.message.reply_text("Adresse invalide, réessaie.")
                return
            user_states.pop(chat_id, None)
            await self.check_ai_score_inline(update, text)

        elif awaiting == "cleanup_inactive_days":
            try:
                days = float(text.strip())
                if days <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("Nombre invalide, envoie un nombre de jours positif (ex: 30).")
                return
            self.data_store.set_cleanup_settings(inactive_days=days)
            await update.message.reply_text(f"✅ Inactivité max réglée à `{days:.0f}` jour(s).", parse_mode="Markdown")
            user_states.pop(chat_id, None)

        elif awaiting == "cleanup_max_losses":
            try:
                n = int(text.strip())
                if n <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("Nombre invalide, envoie un entier positif (ex: 7).")
                return
            self.data_store.set_cleanup_settings(max_consecutive_losses=n)
            await update.message.reply_text(f"✅ Pertes consécutives max réglées à `{n}`.", parse_mode="Markdown")
            user_states.pop(chat_id, None)

        elif awaiting == "withdrawal_min_sol":
            try:
                amount = float(text.strip())
                if amount <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("Montant invalide, envoie un nombre positif (ex: 0.1).")
                return
            self.data_store.set_withdrawal_alert_settings(min_sol=amount)
            await update.message.reply_text(f"✅ Seuil d'alerte retrait SOL réglé à `{amount:.4f}` SOL.", parse_mode="Markdown")
            user_states.pop(chat_id, None)

        elif awaiting == "withdrawal_min_usdc":
            try:
                amount = float(text.strip())
                if amount <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("Montant invalide, envoie un nombre positif (ex: 20).")
                return
            self.data_store.set_withdrawal_alert_settings(min_usdc=amount)
            await update.message.reply_text(f"✅ Seuil d'alerte retrait USDC réglé à `{amount:.2f}` USDC.", parse_mode="Markdown")
            user_states.pop(chat_id, None)

        elif awaiting == "devtransfer_pct":
            try:
                pct = float(text.strip())
                if not (0 < pct <= 100):
                    raise ValueError
            except ValueError:
                await update.message.reply_text("Pourcentage invalide, envoie un nombre entre 0 et 100 (ex: 90).")
                return
            self.data_store.set_dev_transfer_settings(pct_threshold=pct)
            await update.message.reply_text(f"✅ Seuil d'alerte transfert dev réglé à `{pct:.0f}%`.", parse_mode="Markdown")
            user_states.pop(chat_id, None)

        elif awaiting == "position_calc_input":
            parts = text.strip().split()
            if len(parts) != 2:
                await update.message.reply_text("Format attendu : `budget risque%` — exemple `10 2`", parse_mode="Markdown")
                return
            try:
                budget = float(parts[0])
                risk_pct = float(parts[1])
            except ValueError:
                await update.message.reply_text("Les deux valeurs doivent être des nombres. Exemple : `10 2`", parse_mode="Markdown")
                return
            user_states.pop(chat_id, None)
            await self.compute_position_size(update, budget, risk_pct)

        elif awaiting == "gen_wallet_label":
            from wallet_manager import WalletManager
            wm = WalletManager(self.data_store)
            try:
                result = wm.generate_wallet(text.strip())
            except ValueError as e:
                await update.message.reply_text(f"❌ {e}")
                user_states.pop(chat_id, None)
                return

            await update.message.reply_text(
                f"✅ *Wallet '{result['label']}' généré*\n\n"
                f"Adresse publique : `{result['pubkey']}`\n\n"
                f"⚠️ *Clé privée (à sauvegarder MAINTENANT, ne sera plus jamais affichée)* :\n"
                f"`{result['private_key']}`\n\n"
                f"_Copie-la dans un gestionnaire de mots de passe ou un endroit sûr hors-ligne. "
                f"Ce message va être supprimé dans quelques secondes._",
                parse_mode="Markdown",
            )
            user_states.pop(chat_id, None)
            # Auto-suppression du message contenant la clé privée après un court délai
            asyncio.create_task(self._delayed_delete(update.message.chat_id, update.message.message_id + 1, delay=30))

        elif awaiting == "import_wallet_label":
            user_states[chat_id] = {"awaiting": "import_wallet_privatekey", "wallet_label": text.strip()}
            await update.message.reply_text(
                "Colle maintenant la clé privée (base58) de ce wallet.\n"
                "⚠️ Le message sera automatiquement supprimé du chat juste après traitement."
            )

        elif awaiting == "import_wallet_privatekey":
            label = state["wallet_label"]
            from wallet_manager import WalletManager
            wm = WalletManager(self.data_store)

            # Supprime IMMÉDIATEMENT le message contenant la clé privée, avant même
            # de répondre — c'est la mesure de sécurité la plus importante ici.
            try:
                await update.message.delete()
            except Exception as e:
                log.warning(f"Impossible de supprimer le message contenant la clé privée : {e}")

            try:
                result = wm.import_wallet(label, text.strip())
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ Wallet *'{result['label']}'* importé : `{result['pubkey']}`",
                    parse_mode="Markdown",
                )
            except ValueError as e:
                await context.bot.send_message(chat_id=chat_id, text=f"❌ {e}")
            user_states.pop(chat_id, None)

        elif awaiting == "disperse_destination":
            if not _is_solana_address(text):
                await update.message.reply_text("Adresse invalide, réessaie.")
                return
            state["destination"] = text.strip()
            state["awaiting"] = "disperse_amount"
            await update.message.reply_text("Montant en SOL à envoyer ?")

        elif awaiting == "disperse_amount":
            try:
                amount = float(text)
                if amount <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("Montant invalide.")
                return

            wallet_label = state["wallet_label"]
            destination = state["destination"]
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Confirmer l'envoi", callback_data=f"confirmdisperse_{wallet_label}|{destination}|{amount}"),
                InlineKeyboardButton("❌ Annuler", callback_data="menu_wallets"),
            ]])
            await update.message.reply_text(
                f"⚠️ *Confirme l'envoi*\n\nDepuis : `{wallet_label}`\nVers : `{destination}`\n"
                f"Montant : `{amount}` SOL\n\n_Cette action est irréversible._",
                parse_mode="Markdown", reply_markup=keyboard,
            )
            user_states.pop(chat_id, None)

        elif awaiting == "edit_setting":
            await self._apply_setting_edit(update, state, text)
            user_states.pop(chat_id, None)

        elif awaiting == "edit_tp":
            try:
                levels = []
                for part in text.split(","):
                    pct_str, ratio_str = part.split(":")
                    levels.append({"pct": float(pct_str), "sell_ratio": float(ratio_str)})
                self.data_store.update_wallet_settings(state["address"], {"tp_levels": levels})
                await update.message.reply_text(f"✅ {len(levels)} niveau(x) de TP mis à jour.")
            except Exception:
                await update.message.reply_text("Format invalide. Exemple : `50:0.5,150:1.0`", parse_mode="Markdown")
            user_states.pop(chat_id, None)

        elif awaiting == "edit_transfer_ranges":
            try:
                ranges = []
                for part in text.split(",")[:3]:
                    lo_str, hi_str = part.split(":")
                    ranges.append({"min": float(lo_str), "max": float(hi_str)})
                self.data_store.update_wallet_settings(state["address"], {"transfer_ranges": ranges})
                await update.message.reply_text(f"✅ {len(ranges)} range(s) de transfert enregistré(s).")
            except Exception:
                await update.message.reply_text("Format invalide. Exemple : `1.48:1.50,5.66:5.68`", parse_mode="Markdown")
            user_states.pop(chat_id, None)

        elif awaiting == "save_preset_name":
            address = state["address"]
            settings = self.data_store.get_wallet_settings(address)
            self.data_store.save_preset(text, settings)
            await update.message.reply_text(f"💾 Preset `{text}` sauvegardé.", parse_mode="Markdown")
            user_states.pop(chat_id, None)

        elif awaiting == "rename":
            self.data_store.rename_wallet(state["address"], text)
            await update.message.reply_text(f"✅ Renommé en `{text}`.", parse_mode="Markdown")
            user_states.pop(chat_id, None)

        elif awaiting == "quick_buy_custom":
            try:
                amount = float(text)
                await self._execute_quick_buy(update, state["token"], amount)
            except ValueError:
                await update.message.reply_text("Montant invalide.")
            user_states.pop(chat_id, None)

        elif awaiting == "edit_dip":
            try:
                levels = []
                for part in text.split(",")[:3]:
                    drop_str, amount_str = part.split(":")
                    levels.append({"drop_pct": float(drop_str), "amount_sol": float(amount_str)})
                self.data_store.update_wallet_settings(state["address"], {"buy_the_dip_levels": levels})
                await update.message.reply_text(f"✅ {len(levels)} palier(s) Buy The Dip enregistré(s).")
            except Exception:
                await update.message.reply_text("Format invalide. Exemple : `30:0.5,50:1.0`", parse_mode="Markdown")
            user_states.pop(chat_id, None)

        elif awaiting == "edit_multibuy_wallets":
            from wallet_manager import WalletManager
            wm = WalletManager(self.data_store)
            available = set(wm.list_wallets().keys())
            requested = [w.strip() for w in text.split(",") if w.strip()]
            invalid = [w for w in requested if w not in available]

            if invalid:
                await update.message.reply_text(f"⚠️ Wallet(s) introuvable(s) : {', '.join(invalid)}. Réessaie.")
                return

            self.data_store.update_wallet_settings(state["address"], {"multibuy_wallet_labels": requested})
            await update.message.reply_text(f"✅ MultiBuy configuré avec {len(requested)} wallet(s) : {', '.join(requested)}")
            user_states.pop(chat_id, None)

        elif awaiting == "edit_bigbuy":
            try:
                levels = []
                for part in text.split(",")[:3]:
                    range_str, ratio_str = part.split(":")
                    lo_str, hi_str = range_str.split("-")
                    levels.append({"min_sol": float(lo_str), "max_sol": float(hi_str), "sell_ratio": float(ratio_str)})
                self.data_store.update_wallet_settings(state["address"], {"auto_sell_big_buy_levels": levels})
                await update.message.reply_text(f"✅ {len(levels)} palier(s) Auto-Sell on Big Buy enregistré(s).")
            except Exception:
                await update.message.reply_text("Format invalide. Exemple : `2.5-3:0.5,4-10:1.0`", parse_mode="Markdown")
            user_states.pop(chat_id, None)

        elif awaiting == "edit_trailing":
            try:
                tiers = []
                for part in text.split(",")[:5]:
                    mc_str, pct_str = part.split(":")
                    tiers.append({"mc_threshold": float(mc_str), "trailing_pct": float(pct_str)})
                self.data_store.update_wallet_settings(state["address"], {"trailing_sl_tiers": tiers})
                await update.message.reply_text(f"✅ {len(tiers)} palier(s) Trailing SL enregistré(s).")
            except Exception:
                await update.message.reply_text("Format invalide. Exemple : `0:20,100000:30`", parse_mode="Markdown")
            user_states.pop(chat_id, None)

        elif awaiting == "edit_child_hours":
            try:
                hours = float(text)
                s = self.data_store.get_wallet_settings(state["address"])
                cd = s.get("child_defaults", {})
                cd["remove_if_no_launch_h"] = hours
                self.data_store.update_wallet_settings(state["address"], {"child_defaults": cd})
                await update.message.reply_text(f"✅ Remove if no launch = {hours}h")
            except ValueError:
                await update.message.reply_text("Valeur invalide.")
            user_states.pop(chat_id, None)

    async def _apply_setting_edit(self, update, state, text):
        address, field, value_type, allow_none = state["address"], state["field"], state["value_type"], state["allow_none"]

        if allow_none and text.lower() in ("off", "none", "aucun"):
            self.data_store.update_wallet_settings(address, {field: None})
            await update.message.reply_text(f"✅ `{field}` désactivé.", parse_mode="Markdown")
            return

        try:
            value = float(text) if value_type == "float" else int(text)
        except ValueError:
            await update.message.reply_text("Valeur invalide, réessaie.")
            return

        self.data_store.update_wallet_settings(address, {field: value})
        await update.message.reply_text(f"✅ `{field}` = `{value}`", parse_mode="Markdown")

    # ══════════════════════════════════════════════════════════
    # QUICK BUY BY CA
    # ══════════════════════════════════════════════════════════

    async def _offer_quick_buy(self, update: Update, context: ContextTypes.DEFAULT_TYPE, token_mint: str):
        """
        CORRIGÉ suite à un vrai plantage trouvé : le code essayait de définir
        `bot_data` directement sur l'objet Bot (interdit par la bibliothèque,
        AttributeError), et ne sauvegardait en réalité jamais le token nulle
        part — alors que le handler "Custom" (quickbuy_custom) attend de le
        trouver dans context.user_data["quick_buy_token"]. Corrigé pour
        stocker réellement le token au bon endroit.
        """
        keyboard = [
            [
                InlineKeyboardButton("0.1 SOL", callback_data=f"quickbuy_{token_mint}_0.1"),
                InlineKeyboardButton("0.5 SOL", callback_data=f"quickbuy_{token_mint}_0.5"),
            ],
            [
                InlineKeyboardButton("1 SOL", callback_data=f"quickbuy_{token_mint}_1"),
                InlineKeyboardButton("2 SOL", callback_data=f"quickbuy_{token_mint}_2"),
            ],
            [InlineKeyboardButton("✏️ Custom", callback_data="quickbuy_custom")],
            [InlineKeyboardButton("❌ Cancel", callback_data="quickbuy_cancel")],
        ]
        # Sauvegarde réelle du token pour que le cas "Custom" puisse le
        # retrouver ensuite (context.user_data.get("quick_buy_token")).
        context.user_data["quick_buy_token"] = token_mint
        await update.message.reply_text(
            f"🔍 *Token Detected!*\n`{token_mint}`\n\nSélectionne un montant :",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _handle_quick_buy_amount(self, query, data: str):
        _, token_mint, amount_str = data.split("_")
        await self._execute_quick_buy(query, token_mint, float(amount_str))

    async def _execute_quick_buy(self, update_or_query, token_mint: str, amount_sol: float):
        # Achat manuel direct — bypass des réglages de rugger (comportement Quick Buy CA)
        position = await self.trader.open_position(
            token_mint, source_wallet="manual_quick_buy",
            reason=f"Quick Buy manuel ({amount_sol} SOL)",
        )
        text = f"✅ Achat lancé sur `{token_mint[:8]}...`" if position else "❌ Achat impossible (voir logs)."
        if hasattr(update_or_query, "message") and update_or_query.message:
            await update_or_query.message.reply_text(text, parse_mode="Markdown")
        else:
            await update_or_query.edit_message_text(text, parse_mode="Markdown")

    # ══════════════════════════════════════════════════════════
    # VENTE DEPUIS POSITIONS
    # ══════════════════════════════════════════════════════════

    async def _sell_position(self, query, index: int, pct: int):
        positions = self.data_store.state.get("open_positions", [])
        if index >= len(positions):
            await query.edit_message_text("Position introuvable.")
            return
        position = positions[index]
        from backtest import _get_pair_data
        data = await _get_pair_data(position["token_mint"])
        price = float(data.get("priceUsd", position["entry_price"]) or position["entry_price"])
        change_pct = self.trader._pnl_pct(position, price)

        ratio = min(pct / 100, 1.0)
        await self.trader._partial_close(position, price, change_pct, ratio, tp_index=None)
        await self.show_positions(query)

    async def _sell_all(self, query):
        from backtest import _get_pair_data
        for position in list(self.data_store.state.get("open_positions", [])):
            data = await _get_pair_data(position["token_mint"])
            price = float(data.get("priceUsd", position["entry_price"]) or position["entry_price"])
            change_pct = self.trader._pnl_pct(position, price)
            await self.trader._close_remaining(position, price, change_pct, "MANUAL_SELL_ALL")
        await self.show_positions(query)

    async def _find_position_by_mint(self, token_mint: str):
        for position in self.data_store.state.get("open_positions", []):
            if position["token_mint"] == token_mint:
                return position
        return None

    async def _quick_sell_by_mint(self, query, token_mint: str, pct: int):
        """Vente rapide déclenchée depuis les boutons 25/50/75/100% attachés à l'alerte d'achat."""
        position = await self._find_position_by_mint(token_mint)
        if not position:
            await query.answer("Position déjà clôturée ou introuvable.", show_alert=True)
            return

        # CORRIGÉ suite à un vrai bug trouvé : _get_pair_data seul
        # (DexScreener) ne retourne jamais rien pour un token resté sur la
        # bonding curve — le prix retombait alors silencieusement sur
        # position["entry_price"] (0% affiché, PnL réel masqué). Voir le
        # docstring de get_live_price_and_market_cap.
        from backtest import get_live_price_and_market_cap
        live = await get_live_price_and_market_cap(token_mint)
        price = live["price"] or position["entry_price"]
        change_pct = self.trader._pnl_pct(position, price)

        ratio = min(pct / 100, 1.0)
        if ratio >= 0.999:
            await self.trader._close_remaining(position, price, change_pct, "MANUAL_QUICK_SELL")
        else:
            await self.trader._partial_close(position, price, change_pct, ratio, tp_index=None)
        await query.answer(f"Vente {pct}% exécutée.")

    async def _refresh_pnl(self, query, token_mint: str):
        """Bouton 'Tap to Refresh' — édite le message avec le P&L actuel de la position."""
        position = await self._find_position_by_mint(token_mint)
        if not position:
            await query.answer("Position clôturée — plus de P&L à afficher.", show_alert=True)
            return

        # CORRIGÉ (même bug que _quick_sell_by_mint) : voir le docstring de
        # get_live_price_and_market_cap.
        from backtest import get_live_price_and_market_cap
        live = await get_live_price_and_market_cap(token_mint)
        price = live["price"] or position["entry_price"]
        change_pct = self.trader._pnl_pct(position, price)
        value_usd = position["units"] * price

        await query.answer(f"P&L actuel : {change_pct:+.1f}% ({value_usd:.2f}$)", show_alert=True)

    async def _delayed_delete(self, chat_id: int, message_id: int, delay: int = 30):
        await asyncio.sleep(delay)
        try:
            await self.app.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            log.debug(f"Suppression différée du message {message_id} impossible : {e}")

    async def _execute_disperse(self, query, wallet_label: str, destination: str, amount_sol: float):
        from wallet_manager import WalletManager
        wm = WalletManager(self.data_store)
        try:
            signature = await wm.send_sol(wallet_label, destination, amount_sol)
            await query.edit_message_text(
                f"✅ *Envoi confirmé*\n{amount_sol} SOL envoyé vers `{destination[:8]}...`\n"
                f"Tx: `{signature[:20]}...`",
                parse_mode="Markdown",
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Échec de l'envoi : {e}")

    async def _sell_initials_position(self, query, index: int):
        positions = self.data_store.state.get("open_positions", [])
        if index >= len(positions):
            await query.edit_message_text("Position introuvable.")
            return
        position = positions[index]
        from backtest import _get_pair_data
        data = await _get_pair_data(position["token_mint"])
        price = float(data.get("priceUsd", position["entry_price"]) or position["entry_price"])

        done = await self.trader.sell_initials(position, price)
        if not done:
            await query.answer("Pas encore en profit, ou déjà fait — rien à récupérer.", show_alert=True)
        await self.show_positions(query)

    # ══════════════════════════════════════════════════════════
    # UTIL
    # ══════════════════════════════════════════════════════════

    async def _send_or_edit(self, update_or_query, text: str, markup, edit: bool):
        # CORRIGÉ suite à un vrai bug signalé : le bouton "🚀 Démarrer" de
        # l'écran d'accueil (envoyé en photo avec légende, voir cmd_start)
        # ne faisait RIEN au clic — cette fonction appelait toujours
        # edit_message_text() sans vérifier si le message d'origine était une
        # PHOTO. Telegram refuse ce type de modification (il faut
        # edit_message_caption pour une photo, pas edit_message_text) et
        # rejette l'appel silencieusement côté utilisateur — aucune erreur
        # visible, juste "rien ne se passe". Envoie maintenant un nouveau
        # message à la place dans ce cas précis, plutôt que de tenter une
        # édition impossible.
        is_photo_message = edit and hasattr(update_or_query, "message") and getattr(update_or_query.message, "photo", None)

        if edit and hasattr(update_or_query, "edit_message_text") and not is_photo_message:
            await update_or_query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
        elif hasattr(update_or_query, "message") and update_or_query.message:
            await update_or_query.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
        else:
            await update_or_query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
