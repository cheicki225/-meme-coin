"""
════════════════════════════════════════════════════════════════
TRADUCTIONS — Système FR/EN pour le bot Telegram
════════════════════════════════════════════════════════════════
Actuellement appliqué en profondeur au menu principal (la page la plus
visible). Extensible : ajoute simplement de nouvelles clés ici, puis
utilise t("ma_cle", lang) dans telegram_bot.py pour les traduire ailleurs.

Le reste du bot (pages de config détaillées, sous-menus) reste en
français pour l'instant — traduire les dizaines de pages restantes est
un chantier à part, à faire progressivement si besoin.
"""

TRANSLATIONS = {
    "main_menu_mode": {"fr": "Mode", "en": "Mode"},
    "main_menu_autobuy": {"fr": "Auto-Buy global", "en": "Global Auto-Buy"},
    "main_menu_autosell": {"fr": "Auto-Sell global", "en": "Global Auto-Sell"},
    "main_menu_ruggers": {"fr": "Ruggers", "en": "Ruggers"},

    "btn_ruggers": {"fr": "🎯 Ruggeurs", "en": "🎯 Ruggers"},
    "btn_copytrade": {"fr": "📋 Copy Trading", "en": "📋 Copy Trading"},
    "btn_wallets": {"fr": "💰 Portefeuilles", "en": "💰 Wallets"},
    "btn_positions": {"fr": "📊 Positions", "en": "📊 Positions"},
    "btn_analyzecoin": {"fr": "🔍 Analyser un coin", "en": "🔍 Analyze a coin"},
    "btn_analyzewallet": {"fr": "🔎 Analyse de wallet", "en": "🔎 Wallet analysis"},
    "btn_security": {"fr": "🛡️ Sécurité/Scam", "en": "🛡️ Security/Scam"},
    "btn_aiscore": {"fr": "📈 Score IA", "en": "📈 AI Score"},
    "btn_poscalc": {"fr": "💰 Calcul position", "en": "💰 Position Calc"},
    "btn_strategies": {"fr": "📜 Stratégies", "en": "📜 Strategies"},
    "btn_journal": {"fr": "📋 Journal P&L", "en": "📋 P&L Journal"},
    "btn_sessionstats": {"fr": "📊 Stats session", "en": "📊 Session Stats"},
    "btn_toggleai": {"fr": "🤖 IA ON/OFF", "en": "🤖 AI ON/OFF"},
    "btn_alerts": {"fr": "🔔 Alertes ON/OFF", "en": "🔔 Alerts ON/OFF"},
    "btn_stop": {"fr": "🛑 Stop", "en": "🛑 Stop"},
    "btn_more": {"fr": "••• Plus", "en": "••• More"},
    "btn_help": {"fr": "ℹ️ Aide", "en": "ℹ️ Help"},

    "language_button_fr": {"fr": "🌐 Langue : 🇫🇷 FR", "en": "🌐 Language: 🇫🇷 FR"},
    "language_button_en": {"fr": "🌐 Langue : 🇬🇧 EN", "en": "🌐 Language: 🇬🇧 EN"},

    # ── Page config wallet (Ruggeur/Copytrade) ──────────────────────────
    "rugger_not_found": {"fr": "Rugger introuvable.", "en": "Rugger not found."},
    "status_label": {"fr": "Statut", "en": "Status"},
    "status_paused": {"fr": "⏸ PAUSED", "en": "⏸ PAUSED"},
    "status_active": {"fr": "🟢 actif", "en": "🟢 active"},
    "btn_autobuy": {"fr": "Auto-Buy", "en": "Auto-Buy"},
    "btn_autosell": {"fr": "Auto-Sell", "en": "Auto-Sell"},
    "btn_buymode_prefix": {"fr": "⚡ Buy Mode", "en": "⚡ Buy Mode"},
    "btn_tracking_mode": {"fr": "🔔 Tracking Mode", "en": "🔔 Tracking Mode"},
    "btn_buy_config": {"fr": "💰 Buy Config", "en": "💰 Buy Config"},
    "btn_sell_config": {"fr": "📈 Sell Config", "en": "📈 Sell Config"},
    "btn_protection": {"fr": "🛡️ Protection", "en": "🛡️ Protection"},
    "btn_security_ai": {"fr": "🛡️🤖 Security & AI", "en": "🛡️🤖 Security & AI"},
    "btn_snipe_config": {"fr": "🎯 Snipe Config", "en": "🎯 Snipe Config"},
    "btn_maxloss": {"fr": "🛑 Max Loss Counter", "en": "🛑 Max Loss Counter"},
    "btn_savepreset": {"fr": "📋 Save Preset", "en": "📋 Save Preset"},
    "btn_rename": {"fr": "✏️ Rename", "en": "✏️ Rename"},
    "btn_reset": {"fr": "🔄 Reset", "en": "🔄 Reset"},
    "btn_delete": {"fr": "🗑 Delete", "en": "🗑 Delete"},
    "btn_back": {"fr": "← Back", "en": "← Back"},
    "buy_mode_simple": {"fr": "🎯 Simple", "en": "🎯 Simple"},
    "buy_mode_hardcore": {"fr": "🔥 Hardcore", "en": "🔥 Hardcore"},

    "tracking_mode_title": {"fr": "🔔 *Tracking Mode*\n\nChoisis ce que le bot surveille sur cette adresse.",
                             "en": "🔔 *Tracking Mode*\n\nChoose what the bot monitors on this address."},
    "btn_track_creation": {"fr": "🆕 New Token Creations", "en": "🆕 New Token Creations"},
    "btn_track_buy": {"fr": "📦 Track Buys (Copy Trade)", "en": "📦 Track Buys (Copy Trade)"},
    "btn_track_sell": {"fr": "📤 Track Sells", "en": "📤 Track Sells"},
    "btn_buy_on_dev_sell": {"fr": "🔄 Buy on Dev Sell (stub)", "en": "🔄 Buy on Dev Sell (stub)"},

    "buy_mode_title": {
        "fr": "⚡ *Buy Mode*\n\n_Simulé en PAPER — voir doc F Project pour le vrai fonctionnement "
              "(multi-nonce 5 serveurs). Ici, Hardcore simule un taux d'échec de "
              "transaction plus élevé et des frais simulés plus élevés._\n\n"
              "🎯 *Simple* : frais fixes, taux d'échec faible\n"
              "🔥 *Hardcore* : outbid simulé (frais jusqu'à 0.027 SOL), "
              "~25% de transactions simulées en échec (aucun débit dans ce cas)",
        "en": "⚡ *Buy Mode*\n\n_Simulated in PAPER — see F Project docs for the real mechanism "
              "(multi-nonce, 5 servers). Here, Hardcore simulates a higher transaction "
              "failure rate and higher simulated fees._\n\n"
              "🎯 *Simple*: fixed fees, low failure rate\n"
              "🔥 *Hardcore*: simulated outbid (fees up to 0.027 SOL), "
              "~25% of transactions simulated as failed (no debit in that case)",
    },
    "btn_hardcore_active_switch_simple": {"fr": "✅ Hardcore actif — repasser en Simple", "en": "✅ Hardcore active — switch back to Simple"},
    "btn_switch_hardcore": {"fr": "🔥 Passer en Hardcore", "en": "🔥 Switch to Hardcore"},
    "hardcore_confirm_title": {
        "fr": "⚠️ *Confirmation Hardcore Mode*\n\nFrais simulés plus élevés (jusqu'à ~0.027 SOL/tx) et davantage de "
              "transactions simulées en échec (~25%, aucun débit dans ce cas).\n\nConfirme ton choix :",
        "en": "⚠️ *Hardcore Mode Confirmation*\n\nHigher simulated fees (up to ~0.027 SOL/tx) and more "
              "transactions simulated as failed (~25%, no debit in that case).\n\nConfirm your choice:",
    },
    "btn_confirm_hardcore": {"fr": "✅ Yes, I still want Hardcore mode", "en": "✅ Yes, I still want Hardcore mode"},
    "btn_back_to_simple": {"fr": "↩️ I go back to Simple mode", "en": "↩️ I go back to Simple mode"},

    # ── Buy Config ────────────────────────────────────────────────────
    "no_dip_level_configured": {"fr": "Aucun palier configuré", "en": "No tier configured"},
    "dip_level_line": {"fr": "  Niveau {n}: -{drop:.0f}% ATH → +{amount} SOL", "en": "  Level {n}: -{drop:.0f}% ATH → +{amount} SOL"},
    "buy_config_title": {"fr": "💰 *Buy Config*", "en": "💰 *Buy Config*"},
    "buy_the_dip_label": {"fr": "📉 *Buy The Dip*", "en": "📉 *Buy The Dip*"},
    "off_value": {"fr": "off", "en": "off"},
    "btn_buy_amount": {"fr": "✏️ Buy Amount", "en": "✏️ Buy Amount"},
    "btn_snipe_delay": {"fr": "✏️ Snipe Delay (s)", "en": "✏️ Snipe Delay (s)"},
    "btn_min_mc": {"fr": "✏️ Min MC", "en": "✏️ Min MC"},
    "btn_max_mc": {"fr": "✏️ Max MC", "en": "✏️ Max MC"},
    "btn_buy_only_once": {"fr": "🔂 Buy Only Once", "en": "🔂 Buy Only Once"},
    "btn_max_token_age": {"fr": "✏️ Max Token Age (min, copytrade)", "en": "✏️ Max Token Age (min, copytrade)"},
    "btn_follow_cooldown": {"fr": "✏️ Follow Cooldown (s, copytrade)", "en": "✏️ Follow Cooldown (s, copytrade)"},
    "btn_buy_the_dip": {"fr": "📉 Buy The Dip", "en": "📉 Buy The Dip"},
    "btn_multibuy": {"fr": "🧩 MultiBuy", "en": "🧩 MultiBuy"},

    # ── Sell Config ───────────────────────────────────────────────────
    "tp_summary_line": {"fr": "  TP{n}: +{pct:.0f}% → vendre {ratio:.0f}%", "en": "  TP{n}: +{pct:.0f}% → sell {ratio:.0f}%"},
    "bigbuy_level_line": {"fr": "  L{n}: {min}-{max} SOL → vendre {ratio:.0f}%", "en": "  L{n}: {min}-{max} SOL → sell {ratio:.0f}%"},
    "sell_config_title": {"fr": "📈 *Sell Config*", "en": "📈 *Sell Config*"},
    "auto_sell_bigbuy_label": {"fr": "🐳 *Auto-Sell on Big Buy* _(best-effort, ~15s de latence)_",
                                "en": "🐳 *Auto-Sell on Big Buy* _(best-effort, ~15s latency)_"},
    "btn_edit_tp": {"fr": "✏️ Ajouter/Modifier TP", "en": "✏️ Add/Edit TP"},
    "btn_sl_pct": {"fr": "✏️ SL %", "en": "✏️ SL %"},
    "btn_no_activity": {"fr": "✏️ No Activity (s)", "en": "✏️ No Activity (s)"},
    "btn_trailing_sl": {"fr": "📊 Trailing SL", "en": "📊 Trailing SL"},
    "btn_trailing_sl_tiers": {"fr": "📊 Trailing SL — Paliers par MC", "en": "📊 Trailing SL — Tiers by MC"},
    "btn_bigbuy": {"fr": "🐳 Auto-Sell on Big Buy", "en": "🐳 Auto-Sell on Big Buy"},
    "btn_advanced_strategies": {"fr": "🎯 Stratégies avancées", "en": "🎯 Advanced Strategies"},

    # ── Protection ────────────────────────────────────────────────────
    "protection_title": {"fr": "🛡️ *Protection*", "en": "🛡️ *Protection*"},
    "inherit_label": {"fr": "Héritage des Paramètres", "en": "Settings Inheritance"},
    "front_run_sell_beta": {"fr": "_(beta — polling ~15s, pas même-bloc)_", "en": "_(beta — ~15s polling, not same-block)_"},
    "btn_protection_toggle": {"fr": "Protection", "en": "Protection"},
    "btn_mode": {"fr": "🔀 Mode", "en": "🔀 Mode"},
    "btn_fresh_wallet": {"fr": "🆕 Fresh Wallet", "en": "🆕 Fresh Wallet"},
    "btn_keep_address": {"fr": "📌 Keep Address", "en": "📌 Keep Address"},
    "btn_inherit": {"fr": "🔗 Héritage", "en": "🔗 Inheritance"},
    "btn_transfer_ranges": {"fr": "📏 Transfer Ranges", "en": "📏 Transfer Ranges"},
    "btn_front_run_sell": {"fr": "⚡ Front Run Sell", "en": "⚡ Front Run Sell"},
    "btn_front_run_threshold": {"fr": "✏️ Seuil Front Run Sell (%)", "en": "✏️ Front Run Sell Threshold (%)"},
    "btn_child_defaults": {"fr": "⚙️ Child Address Defaults", "en": "⚙️ Child Address Defaults"},
    "child_defaults_title": {"fr": "⚙️ *Child Address Defaults*", "en": "⚙️ *Child Address Defaults*"},
    "child_defaults_subtitle": {"fr": "_Utilisés quand Héritage des Paramètres est OFF sur ce rugger._",
                                 "en": "_Used when Settings Inheritance is OFF on this rugger._"},

    # ── Security & AI ─────────────────────────────────────────────────
    "goplus_configured": {"fr": "✅ configurée", "en": "✅ configured"},
    "goplus_not_configured": {"fr": "❌ aucune clé GOPLUS_API_KEY", "en": "❌ no GOPLUS_API_KEY set"},
    "ai_no_key": {"fr": "❌ aucune clé configurée", "en": "❌ no key configured"},
    "security_ai_title": {"fr": "🛡️🤖 *Security & AI*", "en": "🛡️🤖 *Security & AI*"},
    "goplus_check_label": {"fr": "*Vérification GoPlus*", "en": "*GoPlus Check*"},
    "enabled_on_rugger": {"fr": "Activée sur ce rugger", "en": "Enabled on this rugger"},
    "min_score_label": {"fr": "Score minimum", "en": "Minimum score"},
    "ai_advice_label": {"fr": "*Avis IA*", "en": "*AI Advice*"},
    "enabled_on_rugger_m": {"fr": "Activé sur ce rugger", "en": "Enabled on this rugger"},
    "ai_advice_note": {"fr": "_L'avis IA n'est jamais bloquant seul — juste une notification "
                             "si l'IA n'est pas d'accord avec le backtest._",
                        "en": "_AI advice never blocks on its own — just a notification "
                              "if the AI disagrees with the backtest._"},
    "btn_min_security_score": {"fr": "✏️ Score minimum GoPlus", "en": "✏️ Minimum GoPlus Score"},

    # ── Snipe Config ──────────────────────────────────────────────────
    "snipe_config_title": {"fr": "🎯 *Snipe Config*", "en": "🎯 *Snipe Config*"},
    "wallet_paper_line": {"fr": "Portefeuille : `PAPER` _(simulation, pas de wallet réel)_", "en": "Wallet: `PAPER` _(simulation, no real wallet)_"},
    "label_line": {"fr": "Étiquette", "en": "Label"},
    "snipe_amount_line": {"fr": "Montant du snipe", "en": "Snipe amount"},
    "simulated_settings_note": {"fr": "_⚠️ Les réglages ci-dessous sont simulés — aucune vraie transaction "
                                       "n'est envoyée en mode PAPER, ils n'ont donc aucun effet réel._",
                                 "en": "_⚠️ The settings below are simulated — no real transaction "
                                       "is sent in PAPER mode, so they have no real effect._"},
    "gas_fee_line": {"fr": "Frais de gaz", "en": "Gas fee"},
    "snipe_tip_line": {"fr": "Astuce Snipe (tip)", "en": "Snipe tip"},
    "anti_mev_buy_line": {"fr": "Achat anti-MEV", "en": "Anti-MEV buy"},
    "buy_slippage_line": {"fr": "Slippage d'achat", "en": "Buy slippage"},
    "buy_slippage_pump_line": {"fr": "Slippage d'achat Pump", "en": "Pump buy slippage"},
    "platforms_line": {"fr": "Plateformes", "en": "Platforms"},
    "real_filters_note": {"fr": "_✅ Les filtres ci-dessous sont réellement vérifiés à l'achat "
                                 "(best-effort, voir README pour les limites de précision) :_",
                           "en": "_✅ The filters below are genuinely checked at purchase time "
                                 "(best-effort, see README for precision limits):_"},
    "dev_buy_minmax_line": {"fr": "Achat dev min/max", "en": "Dev buy min/max"},
    "dev_holding_minmax_line": {"fr": "Détention dev min/max", "en": "Dev holding min/max"},
    "speed_settings_note": {"fr": "_⚡ Réglages de VITESSE — réellement actifs en LIVE :_",
                             "en": "_⚡ SPEED settings — genuinely active in LIVE:_"},
    "jito_line": {"fr": "Jito (bundle + tip)", "en": "Jito (bundle + tip)"},
    "max_speed_mode_line": {"fr": "Mode vitesse max", "en": "Max speed mode"},
    "max_speed_mode_note": {"fr": "_(désactive protections MC/pullback/dev si activé)_",
                             "en": "_(disables MC/pullback/dev protections if enabled)_"},
    "btn_snipe_amount": {"fr": "✏️ Montant du snipe", "en": "✏️ Snipe amount"},
    "btn_gas_fee": {"fr": "✏️ Frais de gaz", "en": "✏️ Gas fee"},
    "btn_snipe_tip": {"fr": "✏️ Astuce Snipe (tip)", "en": "✏️ Snipe tip"},
    "btn_anti_mev": {"fr": "Anti-MEV", "en": "Anti-MEV"},
    "btn_buy_slippage": {"fr": "✏️ Slippage d'achat", "en": "✏️ Buy slippage"},
    "btn_buy_slippage_pump": {"fr": "✏️ Slippage Pump", "en": "✏️ Pump slippage"},
    "btn_jito": {"fr": "⚡ Jito", "en": "⚡ Jito"},
    "btn_max_speed_mode": {"fr": "🚀 Mode vitesse max", "en": "🚀 Max speed mode"},
    "btn_dev_buy_min": {"fr": "✏️ Achat dev min (SOL)", "en": "✏️ Dev buy min (SOL)"},
    "btn_dev_buy_max": {"fr": "✏️ Achat dev max (SOL)", "en": "✏️ Dev buy max (SOL)"},
    "btn_dev_holding_min": {"fr": "✏️ Détention dev min (%)", "en": "✏️ Dev holding min (%)"},
    "btn_dev_holding_max": {"fr": "✏️ Détention dev max (%)", "en": "✏️ Dev holding max (%)"},

    # ── Max Loss Counter ──────────────────────────────────────────────
    "maxloss_title": {"fr": "🛑 *Max Loss Counter*", "en": "🛑 *Max Loss Counter*"},
    "current_threshold": {"fr": "Seuil actuel", "en": "Current threshold"},
    "consecutive_losses": {"fr": "pertes consécutives", "en": "consecutive losses"},
    "current_counter": {"fr": "Compteur actuel", "en": "Current counter"},
    "btn_edit_threshold": {"fr": "✏️ Modifier le seuil", "en": "✏️ Edit threshold"},
    "btn_reactivate_autobuy": {"fr": "▶️ Réactiver l'auto-buy", "en": "▶️ Reactivate auto-buy"},

    # ── Ruggers menu ──────────────────────────────────────────────────
    "ruggers_menu_title": {"fr": "🎯 *Ruggeurs* ({count}/{max} au total)", "en": "🎯 *Ruggers* ({count}/{max} total)"},
    "btn_add_rugger": {"fr": "➕ Add Rugger", "en": "➕ Add Rugger"},
    "btn_presets": {"fr": "📋 Presets", "en": "📋 Presets"},

    # ── Copy Trading menu ─────────────────────────────────────────────
    "copytrade_menu_title": {"fr": "📋 *Copy Trading* ({count} wallet(s) suivis)",
                              "en": "📋 *Copy Trading* ({count} wallet(s) followed)"},
    "copytrade_menu_subtitle": {"fr": "_Suit les achats/ventes de wallets choisis, plutôt que de sniper "
                                       "la création d'un token. Contrairement au sniping de dev, aucun "
                                       "backtest automatique n'est fait — qualifie le wallet toi-même "
                                       "avant de l'ajouter (fréquence, win rate, régularité des entrées)._",
                                 "en": "_Follows the buys/sells of chosen wallets, rather than sniping "
                                       "token creation. Unlike dev sniping, no automatic backtest "
                                       "is run — qualify the wallet yourself "
                                       "before adding it (frequency, win rate, entry consistency)._"},
    "btn_add_copytrade": {"fr": "➕ Add Copy Trade", "en": "➕ Add Copy Trade"},
    "mode_buy_display": {"fr": "📦 Buy", "en": "📦 Buy"},
    "mode_sell_display": {"fr": "📤 Sell", "en": "📤 Sell"},

    # ── Portefeuilles ─────────────────────────────────────────────────
    "wallets_title": {"fr": "💰 *Portefeuilles*", "en": "💰 *Wallets*"},
    "wallets_config_required": {"fr": "⚠️ *Configuration requise* : pour créer/gérer des wallets depuis "
                                       "le bot, il faut d'abord une clé de chiffrement.\n\n"
                                       "Lance cette commande une fois sur ton PC :\n"
                                       "```\npython -c \"from wallet_manager import generate_encryption_key; "
                                       "print(generate_encryption_key())\"\n```\n"
                                       "Colle le résultat dans ton `.env` sous `WALLET_ENCRYPTION_KEY=...`, "
                                       "puis relance le bot.",
                                 "en": "⚠️ *Configuration required*: to create/manage wallets from "
                                       "the bot, you first need an encryption key.\n\n"
                                       "Run this command once on your PC:\n"
                                       "```\npython -c \"from wallet_manager import generate_encryption_key; "
                                       "print(generate_encryption_key())\"\n```\n"
                                       "Paste the result into your `.env` as `WALLET_ENCRYPTION_KEY=...`, "
                                       "then restart the bot."},
    "btn_refresh": {"fr": "🔄 Refresh", "en": "🔄 Refresh"},
    "wallets_managed_title": {"fr": "💰 *Portefeuilles* ({count} géré(s))", "en": "💰 *Wallets* ({count} managed)"},
    "active_marker": {"fr": "🟢 ACTIF", "en": "🟢 ACTIVE"},
    "balance_unavailable": {"fr": "(solde indisponible)", "en": "(balance unavailable)"},
    "btn_activate": {"fr": "✅ Activer", "en": "✅ Activate"},
    "no_wallets_managed": {"fr": "_Aucun wallet géré pour l'instant._", "en": "_No wallets managed yet._"},
    "paper_mode_note": {"fr": "_Mode `PAPER` actif — les wallets ci-dessous existent mais aucun trade réel n'est exécuté._",
                         "en": "_`PAPER` mode active — the wallets below exist but no real trade is executed._"},
    "btn_generate": {"fr": "🆕 Générer", "en": "🆕 Generate"},
    "btn_import": {"fr": "📥 Importer", "en": "📥 Import"},

    # ── Positions ─────────────────────────────────────────────────────
    "positions_title": {"fr": "📊 *Positions*", "en": "📊 *Positions*"},
    "no_open_positions": {"fr": "Aucune position ouverte.", "en": "No open positions."},
    "position_line": {"fr": "`{mint}...` — entrée ${price:.8f} ({units:.2f} unités, coût {cost:.2f}$)",
                       "en": "`{mint}...` — entry ${price:.8f} ({units:.2f} units, cost ${cost:.2f})"},
    "btn_sell_initials": {"fr": "💰 Sell Initials (pos {n})", "en": "💰 Sell Initials (pos {n})"},
    "btn_sell_all": {"fr": "🔴 Sell All", "en": "🔴 Sell All"},

    # ── Aide ──────────────────────────────────────────────────────────
    "help_text": {
        "fr": "ℹ️ *Aide*\n\n"
              "*🎯 Ruggeurs* : wallets suivis en mode sniping de dev (achète quand "
              "ils créent un token).\n\n"
              "*📋 Copy Trading* : wallets suivis en mode copie d'achats/ventes.\n\n"
              "*💰 Portefeuilles* : gère les wallets utilisés pour l'exécution "
              "(générer, importer, activer, envoyer des fonds).\n\n"
              "*📊 Positions* : trades ouverts, vente rapide, Sell Initials.\n\n"
              "*🔍 Analyser un coin* : colle une adresse de token pour retrouver "
              "son dev, son financement, et un backtest de rentabilité — sans "
              "rien acheter ni modifier ton monitoring.\n\n"
              "*📋 Journal P&L* : historique de tous tes trades clôturés.\n\n"
              "*••• Plus* : Settings, Parrainages, Presets, Backups.\n\n"
              "💡 *Astuce* : colle directement une adresse de token dans le chat "
              "à tout moment pour un Quick Buy manuel.",
        "en": "ℹ️ *Help*\n\n"
              "*🎯 Ruggers*: wallets tracked in dev-sniping mode (buys when "
              "they create a token).\n\n"
              "*📋 Copy Trading*: wallets tracked in buy/sell copy mode.\n\n"
              "*💰 Wallets*: manage the wallets used for execution "
              "(generate, import, activate, send funds).\n\n"
              "*📊 Positions*: open trades, quick sell, Sell Initials.\n\n"
              "*🔍 Analyze a coin*: paste a token address to find "
              "its dev, its funding, and a profitability backtest — without "
              "buying anything or changing your monitoring.\n\n"
              "*📋 P&L Journal*: history of all your closed trades.\n\n"
              "*••• More*: Settings, Referrals, Presets, Backups.\n\n"
              "💡 *Tip*: paste a token address directly in the chat "
              "any time for a manual Quick Buy.",
    },

    # ── Settings ──────────────────────────────────────────────────────
    "settings_title": {"fr": "⚙️ *Settings*", "en": "⚙️ *Settings*"},
    "btn_gas_fees": {"fr": "⚡ Gas Fees", "en": "⚡ Gas Fees"},
    "btn_notifications": {"fr": "🔔 Notifications", "en": "🔔 Notifications"},
    "global_ai_label": {"fr": "🤖 IA globale", "en": "🤖 Global AI"},
    "gas_fees_title": {"fr": "⚡ *Gas Fees*", "en": "⚡ *Gas Fees*"},
    "gas_fees_note": {"fr": "_Mode PAPER — aucun frais réel n'est prélevé. Valeurs indicatives "
                            "pour te préparer au passage en LIVE (voir doc F Project) :_",
                       "en": "_PAPER mode — no real fee is charged. Indicative values "
                             "to prepare for switching to LIVE (see F Project docs):_"},
    "gas_fees_summary": {"fr": "Priority Fee: 0.001 SOL\nTip: 0.001 SOL\nBribe: 0.02 SOL\nTotal indicatif: ~0.022 SOL/tx",
                          "en": "Priority Fee: 0.001 SOL\nTip: 0.001 SOL\nBribe: 0.02 SOL\nIndicative total: ~0.022 SOL/tx"},
    "notifications_title": {"fr": "🔔 *Notifications*", "en": "🔔 *Notifications*"},
    "notifications_always_on": {"fr": "_Buy Failed / Sell Failed / Bot Errors sont toujours actifs._",
                                 "en": "_Buy Failed / Sell Failed / Bot Errors are always active._"},
}


def t(key: str, lang: str = "fr", **kwargs) -> str:
    """
    Traduit une clé dans la langue demandée. Repli sur le français si la
    langue demandée n'a pas cette clé, puis repli sur la clé elle-même
    (visible, pas de plantage) si la clé n'existe pas du tout — utile
    pendant qu'on étend progressivement la couverture des traductions.
    """
    entry = TRANSLATIONS.get(key)
    if entry is None:
        return key
    text = entry.get(lang, entry.get("fr", key))
    if kwargs:
        text = text.format(**kwargs)
    return text
