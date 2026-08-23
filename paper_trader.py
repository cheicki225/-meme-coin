"""
════════════════════════════════════════════════════════════════
PAPER TRADER — Simulation d'achat/vente (aucun argent réel)
════════════════════════════════════════════════════════════════
Modèle de position : on suit "units" (unités de token abstraites) et
"cost_basis_usd" (argent net investi actuellement attribuable aux
tokens détenus) plutôt qu'un simple ratio, pour pouvoir supporter
correctement le DCA (Buy The Dip) et Sell Initials.

    valeur_position(prix) = units * prix
    P&L%                  = (valeur_position - cost_basis_usd) / cost_basis_usd * 100

Fonctionnalités F Project implémentées ici (voir README pour le niveau
d'approximation de chacune) :
- TP multi-niveaux / SL / No Activity Sell
- Trailing SL multi-paliers par market cap
- Buy The Dip (jusqu'à 3 paliers, DCA sous l'ATH)
- Sell Initials (récupère la mise, garde le reste en free-ride)
- Auto-Sell on Big Buy (BEST-EFFORT — voir _check_big_buy_levels)
- Front Run Sell (BEST-EFFORT/BETA — voir _check_front_run_sell)
- Buy Mode Simple/Hardcore (SIMULÉ — aucune vraie course multi-serveurs)
"""

import time
import random
import logging
import asyncio

import config
import rpc_client
import wallet
import wallet_history
import position_price_stream
from backtest import _get_pair_data, get_bonding_curve_price, get_sol_usd_rate, get_live_price_and_market_cap
import security as security_check

log = logging.getLogger("paper_trader")

# ── Simulation Buy Modes (cosmétique — voir doc buy-modes.md) ─────
HARDCORE_FAIL_RATE = 0.25          # ~20-30% de fails documentés en Hardcore
HARDCORE_MAX_FEE_SOL = 0.027       # cap documenté (priority+tip)
SIMPLE_TYPICAL_FEE_SOL = 0.003     # ordre de grandeur pour un mode "tranquille"


def _build_position_keyboard(token_mint: str):
    """
    Boutons de vente rapide + refresh + liens externes, attachés directement
    à l'alerte d'achat (comme F Project : 25/50/75/100%, X%, Refresh, Axiom, TX).
    """
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except ImportError:
        return None

    # CORRIGÉ suite à un vrai lien cassé signalé : "/meme/{mint}" ne menait
    # pas à la bonne page. Format confirmé fonctionnel par Cheicki :
    # "/t/{mint}?chain=sol".
    axiom_url = f"https://axiom.trade/t/{token_mint}?chain=sol"
    solscan_url = f"https://solscan.io/token/{token_mint}"

    keyboard = [
        [
            InlineKeyboardButton("25%", callback_data=f"qsell_{token_mint}_25"),
            InlineKeyboardButton("50%", callback_data=f"qsell_{token_mint}_50"),
            InlineKeyboardButton("75%", callback_data=f"qsell_{token_mint}_75"),
            InlineKeyboardButton("100%", callback_data=f"qsell_{token_mint}_100"),
        ],
        [InlineKeyboardButton("🔄 Refresh PnL", callback_data=f"refreshpnl_{token_mint}")],
        [
            InlineKeyboardButton("📊 Axiom ↗", url=axiom_url),
            InlineKeyboardButton("🔗 Solscan ↗", url=solscan_url),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


class PaperTrader:
    def __init__(self, data_store, notifier=None, price_stream=None):
        self.data_store = data_store
        self.notifier = notifier
        # AJOUTÉ (demande explicite) : surveillance de position en WebSocket
        # (accountSubscribe) au lieu du sondage RPC répété — voir
        # position_price_stream.py pour le détail. None = fonctionne quand
        # même (jamais requis), _monitor_position retombe alors uniquement
        # sur le sondage RPC classique, comme avant ce changement.
        self.price_stream = price_stream

    # ══════════════════════════════════════════════════════════
    # PRIMITIVES D'EXÉCUTION — surchargées par LiveTrader pour le vrai trading
    # ══════════════════════════════════════════════════════════

    async def _execute_buy(self, token_mint: str, entry_price: float, settings: dict) -> dict:
        """
        Version PAPER : aucune vraie transaction, conversion directe au prix
        coté sans slippage simulé. buy_amount_sol de settings est converti en
        USD via backtest.get_sol_usd_rate() (taux en direct, voir ce module)
        pour rester cohérent avec le dimensionnement réellement configuré
        par wallet (plutôt que config.POSITION_SIZE_USD seul, qui ne
        reflétait pas les réglages par rugger).
        Retourne None seulement si l'achat doit être annulé (jamais en PAPER).
        """
        buy_amount_sol = settings.get("buy_amount_sol", 0.1)
        cost_basis_usd = buy_amount_sol * await get_sol_usd_rate()
        units = cost_basis_usd / entry_price
        return {"units": units, "cost_basis_usd": cost_basis_usd, "actual_price": entry_price, "signature": None}

    async def _execute_sell(self, token_mint: str, units: float, current_price: float, settings: dict) -> dict:
        """Version PAPER : conversion directe au prix coté, aucun slippage simulé.
        Retourne None seulement si la vente doit être annulée (jamais en PAPER)."""
        proceeds_usd = units * current_price
        return {"proceeds_usd": proceeds_usd, "signature": None}

    # ══════════════════════════════════════════════════════════
    # OUVERTURE DE POSITION
    # ══════════════════════════════════════════════════════════

    async def open_position(self, token_mint: str, source_wallet: str, reason: str, creation_slot: int = None,
                             source_entry_market_cap: float = None):
        # CORRIGÉ suite à un vrai cas observé : SOL (et USDC par précaution)
        # ne sont jamais un "token" à copier — voir le fix correspondant dans
        # copytrade_listener._classify_transaction (cause racine, qui exclut
        # déjà ces mints en amont). Second filet ici, indépendant : si un
        # AUTRE chemin d'appel envoyait un jour un de ces mints par erreur,
        # get_bonding_curve_price échouerait silencieusement (pas de bonding
        # curve pour ces mints) et le repli DexScreener pourrait renvoyer un
        # market cap absurde pour un mint aussi largement pairé que SOL/USDC
        # (observé en pratique : 80+ millions de $ pour le mint SOL natif).
        if token_mint in (config.SOL_MINT, wallet.USDC_MINT):
            log.warning(f"🛑 open_position appelé avec un mint non-memecoin ({token_mint[:8]}...), achat refusé.")
            return None

        state = self.data_store.state
        settings = self.data_store.get_wallet_settings(source_wallet)

        if not self.data_store.is_auto_buy_active(source_wallet):
            log.info(f"⏭️  Auto-buy désactivé ou en pause pour {source_wallet[:8]}..., skip.")
            if self.notifier:
                await self.notifier.notify(
                    "buy_skipped",
                    f"⏭️ *Buy Skipped*\nWallet: `{source_wallet[:8]}...`\nRaison: auto-buy désactivé ou pausé (Max Loss Counter)",
                )
            return None

        # CORRIGÉ suite à un vrai bug trouvé (achats bloqués silencieusement,
        # sans AUCUN moyen de le savoir ni de le réinitialiser) : malgré son
        # nom "pertes de SESSION", session_losses_usd ne se remettait JAMAIS
        # à zéro nulle part dans le code — ni au redémarrage, ni chaque jour,
        # ni via un bouton Telegram. Une fois 150$ de pertes CUMULÉES DEPUIS
        # LA TOUTE PREMIÈRE UTILISATION DU BOT atteints, plus AUCUN achat
        # n'était plus jamais possible, en silence, sans qu'aucun message
        # n'explique pourquoi côté Telegram. Remise à zéro automatique
        # quotidienne ajoutée ici, cohérente avec le nom "session".
        last_reset = state.get("session_losses_reset_at", 0)
        if time.time() - last_reset >= 86400:  # 24h
            state["session_losses_usd"] = 0.0
            state["session_losses_reset_at"] = time.time()
            self.data_store.save()

        if state.get("session_losses_usd", 0) >= config.CIRCUIT_BREAKER_USD:
            log.warning("🛑 Circuit breaker actif — aucune nouvelle position ouverte.")
            return None

        if settings.get("buy_only_once", True) and self.data_store.has_already_bought(source_wallet, token_mint):
            log.info(f"⏭️  {source_wallet[:8]}... a déjà acheté {token_mint[:8]}... (buy_only_once actif), skip.")
            if self.notifier:
                await self.notifier.notify(
                    "buy_skipped",
                    f"⏭️ *Buy Skipped* (buy only once)\nWallet: `{source_wallet[:8]}...`\n"
                    f"Token: `{token_mint[:8]}...`\nCe wallet a déjà acheté CE token précis — "
                    f"on ne copie pas un rachat du même token, mais tout nouveau token reste suivi normalement.",
                )
            return None

        # Buy Mode : simulation d'échec de transaction en Hardcore (voir doc buy-modes.md)
        # "Une transaction qui fail = aucun SOL prélevé" -> on ne débite rien, on log juste le skip.
        buy_mode = settings.get("buy_mode", "simple")
        simulated_fee_sol = SIMPLE_TYPICAL_FEE_SOL
        if buy_mode == "hardcore":
            simulated_fee_sol = round(random.uniform(0.01, HARDCORE_MAX_FEE_SOL), 4)
            if random.random() < HARDCORE_FAIL_RATE:
                log.info(f"🔥 [Hardcore] Transaction simulée en échec (CU exceeded) — aucun SOL débité.")
                if self.notifier:
                    await self.notifier.notify(
                        "buy_failed",
                        f"❌ *Buy Failed* (Hardcore — compute budget exceeded, simulé)\n"
                        f"Token: `{token_mint[:8]}...`\nAucun montant débité.",
                    )
                return None

        delay = settings.get("snipe_delay_s", 0)
        if delay > 0:
            log.info(f"⏱️  Snipe delay {delay}s avant achat sur {token_mint[:8]}...")
            await asyncio.sleep(delay)

        # ── Priorité vitesse : saute le fetch DexScreener pré-achat ────────
        # Économise un aller-retour réseau complet avant de lancer l'achat Jupiter.
        # Coût : aucune protection min/max MC, pullback, ou filtre dev n'est
        # appliquée pour ce wallet — assume que tu acceptes ce compromis.
        # Uniquement utile en LIVE (en PAPER, aucun vrai avantage de vitesse à
        # gagner, et le calcul du P&L a besoin d'un prix réel).
        skip_speed = settings.get("skip_price_check_for_speed") and config.TRADING_MODE == "LIVE"

        if skip_speed:
            log.info(f"⚡ Mode vitesse max sur {token_mint[:8]}... — protections MC/pullback/dev désactivées.")
            entry_price = 0.0  # sera remplacé par le prix réel d'exécution Jupiter (actual_price)
            market_cap = 0.0
        else:
            # AJOUTÉ (demande explicite) : n'achète en copy trade que si le
            # token a moins de max_token_age_at_buy_s secondes au moment de
            # l'achat détecté — un wallet suivi qui achète un token qui
            # traîne déjà depuis un moment n'est pas le signal qu'on veut
            # copier. Placé ici (pas avant le bloc skip_speed) pour rester
            # cohérent avec le compromis déjà documenté de ce mode : aucune
            # protection supplémentaire, aucun appel réseau en plus, priorité
            # absolue à la vitesse d'exécution LIVE.
            max_age_s = settings.get("max_token_age_at_buy_s")
            if max_age_s:
                creation_time = await wallet_history.get_token_creation_time(token_mint)
                if creation_time is None:
                    # CORRIGÉ suite à un vrai cas observé en conditions réelles :
                    # le fail-closed d'origine ("indéterminable = skip") bloquait
                    # en pratique la QUASI-TOTALITÉ des copy trades, pas
                    # seulement les tokens trop vieux. Cause : un token acheté
                    # en copy trade très peu de temps après sa création est
                    # justement le cas où getSignaturesForAddress n'a souvent
                    # AUCUNE signature à retourner encore (propagation on-chain
                    # pas terminée) — indéterminable est donc en réalité un
                    # signal plutôt EN FAVEUR de "token très frais", pas contre.
                    # Fail-open maintenant : on achète quand même, avec une
                    # notif claire pour rester visible sur ce cas plutôt que de
                    # bloquer silencieusement le copy trading dans son ensemble.
                    log.info(f"⚠️  Âge du token {token_mint[:8]}... indéterminable — achat maintenu (probable token très frais).")
                    if self.notifier:
                        await self.notifier.notify(
                            "buy_skipped",
                            f"⚠️ *Âge indéterminable* (achat maintenu)\nToken: `{token_mint[:8]}...`\n"
                            f"Impossible de confirmer l'âge exact — probablement très frais, achat non bloqué.",
                        )
                else:
                    age_s = time.time() - creation_time
                    if age_s > max_age_s:
                        log.info(f"⏭️  Token {token_mint[:8]}... âgé de {age_s:.0f}s (> {max_age_s:.0f}s), skip.")
                        if self.notifier:
                            await self.notifier.notify(
                                "buy_skipped",
                                f"⏭️ *Buy Skipped* (token trop vieux)\nToken: `{token_mint[:8]}...`\n"
                                f"Âge: {age_s:.0f}s (max configuré: {max_age_s:.0f}s)",
                            )
                        return None

            # CORRIGÉ suite à un vrai échec signalé : "Buy Failed - prix
            # d'entrée indisponible" survenait systématiquement sur des
            # tokens tout juste créés (snipe_delay_s=0 par défaut). Cause
            # confirmée : DexScreener n'indexe PAS un token Pump.fun tant
            # qu'il est sur la bonding curve (seulement après migration
            # vers PumpSwap) — aucun nombre de tentatives n'y change rien.
            # Priorité 1 : lecture directe du compte bonding curve on-chain
            # (get_bonding_curve_price, voir backtest.py) — disponible dès
            # T+0, sans dépendre d'aucun indexeur. Priorité 2 (repli) :
            # DexScreener, meilleure source pour un token déjà migré
            # (liquidité/volume réels du pool AMM, que la bonding curve ne
            # reflète plus une fois "complete").
            onchain_price = await get_bonding_curve_price(token_mint)
            if onchain_price and not onchain_price.get("complete"):
                entry_price = onchain_price["price_sol"] * await get_sol_usd_rate()  # normalise en $/token comme priceUsd DexScreener (taux en direct, voir backtest.get_sol_usd_rate)
                market_cap = onchain_price["market_cap_usd"]  # déjà calculé avec le taux en direct dans get_bonding_curve_price
            else:
                entry_data = await _get_pair_data(token_mint)
                entry_price = float(entry_data.get("priceUsd", 0) or 0)
                market_cap = float(entry_data.get("marketCap", entry_data.get("fdv", 0)) or 0)

            if entry_price <= 0:
                log.warning(f"Prix d'entrée invalide pour {token_mint}, position annulée.")
                if self.notifier:
                    await self.notifier.notify(
                        "buy_failed",
                        f"❌ *Buy Failed*\nToken: `{token_mint[:8]}...`\nRaison: prix d'entrée indisponible",
                    )
                return None

        # ── Achat sur pullback avec timeout ────────────────────────────
        # Interdit d'acheter au-dessus de pullback_max_mc. Si c'est le cas,
        # attend jusqu'à pullback_timeout_s que le market cap redescende à ce
        # niveau ou en dessous, et achète dès que c'est le cas ("la seconde
        # suivante"). Si le délai expire sans repli, on abandonne ce token.
        # Ignoré en mode vitesse max (skip_speed) puisqu'on n'a pas de market
        # cap connu à ce stade.
        if not skip_speed and settings.get("pullback_entry_enabled") and market_cap > settings.get("pullback_max_mc", 4500):
            pullback_result = await self._wait_for_pullback(
                token_mint, settings.get("pullback_max_mc", 4500), settings.get("pullback_timeout_s", 5),
            )
            if pullback_result is None:
                log.info(
                    f"⏭️  Pullback jamais atteint sous {settings.get('pullback_max_mc', 4500):.0f}$ MC "
                    f"après {settings.get('pullback_timeout_s', 5)}s — token abandonné."
                )
                if self.notifier:
                    await self.notifier.notify(
                        "buy_skipped",
                        f"⏭️ *Buy Skipped* (pullback non atteint)\nToken: `{token_mint[:8]}...`\n"
                        f"Le market cap n'est jamais redescendu sous "
                        f"{settings.get('pullback_max_mc', 4500):.0f}$ à temps.",
                    )
                return None
            # Réévalue le prix/market cap réel au moment du pullback pour l'achat
            entry_price = pullback_result["price"]
            market_cap = pullback_result["market_cap"]
            log.info(f"✅ Pullback atteint sur {token_mint[:8]}... — achat à {market_cap:.0f}$ MC.")

        if not skip_speed:
            min_mc = settings.get("min_market_cap")
            max_mc = settings.get("max_market_cap")
            if min_mc is not None and market_cap < min_mc:
                log.info(f"⏭️  Market cap {market_cap:.0f}$ < min {min_mc:.0f}$, achat refusé (protection).")
                if self.notifier:
                    await self.notifier.notify(
                        "buy_skipped",
                        f"⏭️ *Buy Skipped* (market cap trop bas)\nWallet: `{source_wallet[:8]}...`\n"
                        f"Token: `{token_mint[:8]}...`\nMarket cap: {market_cap:.0f}$ (min configuré: {min_mc:.0f}$)\n"
                        f"L'adresse a acheté/créé ce token mais on ne l'a pas suivi.",
                    )
                return None
            if max_mc is not None and market_cap > max_mc:
                log.info(f"⏭️  Market cap {market_cap:.0f}$ > max {max_mc:.0f}$, achat refusé (protection).")
                if self.notifier:
                    await self.notifier.notify(
                        "buy_skipped",
                        f"⏭️ *Buy Skipped* (market cap trop haut)\nWallet: `{source_wallet[:8]}...`\n"
                        f"Token: `{token_mint[:8]}...`\nMarket cap: {market_cap:.0f}$ (max configuré: {max_mc:.0f}$)\n"
                        f"L'adresse a acheté/créé ce token mais on ne l'a pas suivi (protection).",
                    )
                return None

        # ── Filtres sur le comportement du dev (achat initial + détention %) ─
        # Best-effort : approxime l'achat du dev via son solde de tokens juste
        # après détection, converti en équivalent SOL via SOL_USD_RATE (voir
        # config.py — pas de vrai taux de change temps réel câblé).
        # Ignoré en mode vitesse max (skip_speed) — nécessite entry_price réel.
        dev_min = settings.get("dev_buy_min_sol")
        dev_max = settings.get("dev_buy_max_sol")
        hold_min = settings.get("dev_holding_min_pct")
        hold_max = settings.get("dev_holding_max_pct")
        if not skip_speed and (dev_min is not None or dev_max is not None or hold_min is not None or hold_max is not None):
            dev_stats = await self._check_dev_behavior(source_wallet, token_mint, entry_price)
            if dev_stats:
                if dev_min is not None and dev_stats["dev_buy_sol"] < dev_min:
                    log.info(f"⏭️  Achat dev {dev_stats['dev_buy_sol']:.2f} SOL < min {dev_min}, achat refusé.")
                    return None
                if dev_max is not None and dev_stats["dev_buy_sol"] > dev_max:
                    log.info(f"⏭️  Achat dev {dev_stats['dev_buy_sol']:.2f} SOL > max {dev_max}, achat refusé.")
                    return None
                if hold_min is not None and dev_stats["dev_holding_pct"] < hold_min:
                    log.info(f"⏭️  Détention dev {dev_stats['dev_holding_pct']:.1f}% < min {hold_min}%, achat refusé.")
                    return None
                if hold_max is not None and dev_stats["dev_holding_pct"] > hold_max:
                    log.info(f"⏭️  Détention dev {dev_stats['dev_holding_pct']:.1f}% > max {hold_max}%, achat refusé.")
                    return None

        # ── Vérification sécurité GoPlus (si activée pour ce wallet) ──
        if settings.get("security_check_enabled"):
            security = await security_check.check_token_security(token_mint)
            if security.get("is_honeypot"):
                log.warning(f"🚨 Honeypot détecté sur {token_mint[:8]}... — achat annulé.")
                if self.notifier:
                    await self.notifier.notify(
                        "buy_skipped",
                        f"🚨 *Buy Skipped* — Honeypot détecté\nToken: `{token_mint[:8]}...`",
                    )
                return None
            min_score = settings.get("min_security_score", 60)
            if security.get("security_score", 50) < min_score:
                log.info(
                    f"⏭️  Score sécurité {security.get('security_score')} < {min_score} "
                    f"pour {token_mint[:8]}..., achat refusé."
                )
                if self.notifier:
                    await self.notifier.notify(
                        "buy_skipped",
                        f"⏭️ *Buy Skipped* — Score sécurité insuffisant\n"
                        f"Token: `{token_mint[:8]}...`\nScore: {security.get('security_score')}/100",
                    )
                return None

        buy_result = await self._execute_buy(token_mint, entry_price, settings)
        if buy_result is None:
            log.warning(f"Achat annulé/échoué pour {token_mint[:8]}...")
            return None

        cost_basis_usd = buy_result["cost_basis_usd"]
        units = buy_result["units"]
        actual_entry_price = buy_result.get("actual_price", entry_price)
        buy_signature = buy_result.get("signature")

        position = {
            "token_mint": token_mint,
            "source_wallet": source_wallet,
            "entry_price": actual_entry_price,
            "entry_market_cap": market_cap,
            "entry_time": time.time(),
            "cost_basis_usd": cost_basis_usd,
            "units": units,
            "settings": settings,
            "ath_market_cap": market_cap,
            "tp_levels_hit": [],
            "dip_levels_hit": [],
            "big_buy_levels_hit": [],
            "initials_sold": False,
            "status": "open",
            "reason": reason,
            "buy_mode": buy_mode,
            "simulated_fees_sol": simulated_fee_sol,
            "tx_signatures": [buy_signature] if buy_signature else [],
            "total_sol_invested": settings.get("buy_amount_sol", 0.1),
            "total_sol_received": 0.0,
            "mc_trailing_armed": False,
            "profit_trail_armed": False,
            "profit_trail_floor": None,
        }

        self.data_store.state.setdefault("open_positions", []).append(position)
        if settings.get("buy_only_once", True):
            self.data_store.mark_bought(source_wallet, token_mint)
        self.data_store.save()

        log.info(
            f"📈 [{config.TRADING_MODE}] Position ouverte sur {token_mint} @ ${actual_entry_price:.8f} "
            f"(mcap {market_cap:.0f}$, mode {buy_mode}, {reason})"
        )

        # ── Télémétrie de vitesse (bloc+0 / bloc+N) ────────────────────
        # Uniquement pertinent en LIVE (buy_signature = vraie transaction on-chain).
        # Compare le slot de création du token au slot de notre achat, comme
        # Faomo le fait manuellement sur Solscan dans les vidéos.
        speed_line = ""
        if buy_signature and creation_slot is not None:
            try:
                our_slot = await self._get_transaction_slot(buy_signature)
                if our_slot is not None:
                    delta = our_slot - creation_slot
                    position["block_delta"] = delta
                    speed_line = f"\nVitesse: bloc+{delta} (création: {creation_slot}, achat: {our_slot})"
                    log.info(f"⚡ Vitesse d'exécution : bloc+{delta} sur {token_mint[:8]}...")
            except Exception as e:
                log.debug(f"Impossible de calculer la télémétrie de vitesse : {e}")

        if self.notifier:
            sig_line = f"\nTx: `{buy_signature[:16]}...`" if buy_signature else ""
            keyboard = _build_position_keyboard(token_mint)

            # AJOUTÉ suite à une demande explicite : identifie si cet achat
            # vient d'un DEV suivi (mode track_creation, "sniper") ou d'un
            # TRADER copié (mode track_buy, "copy trading"), et affiche
            # l'adresse complète du wallet déclencheur + du token — plus
            # juste des adresses tronquées à 8 caractères.
            source_entry = self.data_store.state["monitored_dev_wallets"].get(source_wallet, {})
            source_mode = source_entry.get("mode", "track_creation")
            if source_mode == "track_creation":
                source_line = f"🎯 Source: *Sniper Dev*\nWallet dev: `{source_wallet}`\n"
            elif source_mode in ("track_buy", "track_sell", "buy_on_dev_sell"):
                source_line = f"📋 Source: *Copy Trading*\nWallet copié: `{source_wallet}`\n"
            else:
                source_line = f"Wallet source: `{source_wallet}`\n"

            # Nom du token si disponible — souvent indisponible pour un
            # token tout juste sniped (pas encore indexé sur DexScreener,
            # voir les nombreux cas déjà rencontrés). Repli honnête plutôt
            # que d'inventer un nom.
            try:
                pair_data = await _get_pair_data(token_mint)
                token_symbol = (pair_data.get("baseToken") or {}).get("symbol")
            except Exception:
                token_symbol = None
            token_line = f"Token: *{token_symbol}*\n" if token_symbol else "Token: _nom indisponible (token trop récent)_\n"

            # AJOUTÉ (demande explicite) : distingue clairement MON market
            # cap d'entrée de celui du wallet SOURCE (quand disponible) —
            # avant, "Market cap: X$" ne précisait pas de qui il s'agissait,
            # et le MC du wallet source n'était affiché nulle part.
            mc_lines = f"Market cap (mon entrée): {market_cap:.0f}$\n"
            if source_entry_market_cap:
                source_label = source_entry.get("label", source_wallet[:8] + "...")
                mc_lines += f"Market cap (entrée {source_label}): {source_entry_market_cap:.0f}$\n"

            await self.notifier.notify(
                "buy_confirmed",
                f"✅ *Buy Confirmed* ({buy_mode}, {config.TRADING_MODE})\n"
                f"{source_line}"
                f"{token_line}"
                f"`{token_mint}`\n"
                f"Montant: {cost_basis_usd:.2f}$ (~{settings.get('buy_amount_sol')} SOL)\n"
                f"{mc_lines}"
                f"{sig_line}{speed_line}\nRaison: {reason}",
                reply_markup=keyboard,
            )
        asyncio.create_task(self._monitor_position(position))
        return position

    async def _get_transaction_slot(self, signature: str) -> int:
        """Récupère le slot (numéro de bloc) réel d'une transaction confirmée."""
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTransaction",
            "params": [signature, {"encoding": "json", "maxSupportedTransactionVersion": 0}],
        }
        result = await rpc_client.rpc_post(payload, timeout=10)
        if isinstance(result, dict):
            return result.get("slot")
        return None

    async def _wait_for_pullback(self, token_mint: str, max_mc: float, timeout_s: float) -> dict:
        """
        Poll le prix/market cap du token toutes les secondes jusqu'à ce qu'il
        redescende à max_mc ou moins, ou jusqu'à expiration du délai.
        Retourne {"price": float, "market_cap": float} dès que le seuil est
        atteint, ou None si le délai expire sans repli.
        """
        elapsed = 0.0
        poll_interval = 1.0
        while elapsed < timeout_s:
            data = await _get_pair_data(token_mint)
            price = float(data.get("priceUsd", 0) or 0)
            market_cap = float(data.get("marketCap", data.get("fdv", 0)) or 0)

            if price > 0 and market_cap <= max_mc:
                return {"price": price, "market_cap": market_cap}

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        return None

    # ══════════════════════════════════════════════════════════
    # SURVEILLANCE DE POSITION
    # ══════════════════════════════════════════════════════════

    async def _monitor_position(self, position: dict, poll_interval: int = 15, max_duration_s: int = 3600):
        settings = position["settings"]
        elapsed = 0
        no_activity_elapsed = 0

        # ── Intervalle de sondage adaptatif ──────────────────────────────
        # Front Run Sell et Auto-Sell on Big Buy sont sensibles à la latence
        # (best-effort documenté : détection par polling, pas un vrai flux
        # temps réel). Un sondage plus rapide (5s au lieu de 15s) réduit
        # concrètement la fenêtre de réaction — reste du polling, pas du
        # temps réel, mais 3x plus réactif quand ces fonctions sont actives.
        fast_checks_active = bool(settings.get("front_run_sell_enabled")) or bool(settings.get("auto_sell_big_buy_levels"))
        effective_poll_interval = 5 if fast_checks_active else poll_interval
        if fast_checks_active:
            log.info(
                f"⚡ Sondage accéléré (5s) activé sur {position['token_mint'][:8]}... "
                f"(Front Run Sell et/ou Auto-Sell on Big Buy actifs)"
            )

        # AJOUTÉ (demande explicite) : abonnement WebSocket (accountSubscribe)
        # sur le compte bonding curve de ce token — voir position_price_stream.py.
        # Remplace le sondage RPC répété par des mises à jour poussées par
        # Helius, très largement moins coûteuses. self.price_stream est None
        # si non câblé (ex: tests, ou si le module ne démarre pas faute de
        # clé Helius) — dans ce cas on retombe intégralement sur l'ancien
        # comportement de sondage, rien ne casse.
        queue = await self.price_stream.subscribe(position["token_mint"]) if self.price_stream else None
        last_ws_price_usd = None
        last_ws_market_cap = None
        last_ws_ts = 0.0

        try:
            while elapsed < max_duration_s and position["units"] > 0:
                await asyncio.sleep(effective_poll_interval)
                elapsed += effective_poll_interval

                # CORRIGÉ suite à un vrai bug trouvé : "Auto-Sell global" (menu
                # principal, bouton à côté d'"Auto-Buy global") changeait bien
                # l'état affiché et sauvegardait la valeur, mais
                # is_auto_sell_active() — la fonction censée le vérifier —
                # n'était appelée NULLE PART dans le fichier qui exécute les
                # ventes. Le bouton était décoratif : ON ou OFF, rien ne
                # changeait réellement. Contrairement à "Auto-Buy global", déjà
                # bien branché (voir open_position ci-dessus).
                if not self.data_store.is_auto_sell_active(position["source_wallet"]):
                    continue  # vente automatique désactivée — la position reste ouverte, on continue juste de suivre son prix

                # AJOUTÉ : draine la queue de mises à jour poussées par le
                # WebSocket (non-bloquant) — ne garde que la plus récente, les
                # éventuelles mises à jour intermédiaires n'ont pas besoin
                # d'être traitées une par une, seul l'état ACTUEL nous
                # intéresse pour évaluer les conditions de sortie.
                if queue:
                    latest_push = None
                    while not queue.empty():
                        try:
                            latest_push = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    if latest_push:
                        last_ws_price_usd = latest_push["price_usd"]
                        last_ws_market_cap = latest_push["market_cap_usd"]
                        last_ws_ts = time.time()

                # CORRIGÉ suite à un vrai bug trouvé : cette boucle utilisait
                # SEULEMENT _get_pair_data (DexScreener), qui n'indexe jamais un
                # token resté sur la bonding curve (voir le docstring de
                # get_live_price_and_market_cap) — price=0 en continu, "continue"
                # immédiat ci-dessous, donc AUCUNE logique de sortie ne
                # s'exécutait jamais pour ces positions (la majorité des entrées
                # copy trade). need_pair_data=True seulement si no_activity_sell
                # est configuré pour ce wallet — c'est le seul bloc ci-dessous
                # qui a besoin des champs DexScreener (txns.m5).
                needs_pair_data = bool(settings.get("no_activity_sell_s"))
                ws_price_fresh = last_ws_price_usd is not None and (time.time() - last_ws_ts) < position_price_stream.SAFETY_POLL_TIMEOUT_S

                if ws_price_fresh and not needs_pair_data:
                    # AJOUTÉ : prix reçu récemment via WebSocket, et personne
                    # n'a besoin des champs DexScreener (no_activity_sell
                    # inactif) — aucun appel réseau nécessaire ce cycle.
                    price = last_ws_price_usd
                    market_cap = last_ws_market_cap
                    data = {}
                elif ws_price_fresh and needs_pair_data:
                    # Prix WS à jour, mais no_activity_sell a quand même besoin
                    # de txns.m5 — DexScreener seul (pas de RPC Helius, donc pas
                    # de coût supplémentaire côté Helius), sans redemander le
                    # prix bonding curve qu'on a déjà via le WS.
                    data = await _get_pair_data(position["token_mint"])
                    price = last_ws_price_usd
                    market_cap = last_ws_market_cap
                else:
                    # AUCUNE mise à jour WS récente (pas de price_stream, pas
                    # encore de premier message, ou plus de SAFETY_POLL_TIMEOUT_S
                    # sans nouvelle — connexion WS potentiellement perdue) :
                    # filet de sécurité, on retombe sur l'ancien sondage complet.
                    live = await get_live_price_and_market_cap(position["token_mint"], need_pair_data=needs_pair_data)
                    price = live["price"]
                    market_cap = live["market_cap"]
                    data = live["pair_data"]

                if price <= 0:
                    continue

                position["ath_market_cap"] = max(position["ath_market_cap"], market_cap)
                change_pct = self._pnl_pct(position, price)

                # ── Vente auto sur repli de market cap après un pic ────────
                # (ex: si le token a dépassé 5k MC puis retombe à 2.5k, vente totale)
                if settings.get("mc_trailing_enabled"):
                    if await self._check_mc_trailing_sell(position, price, market_cap, change_pct, settings):
                        return

                # ── Trailing stop sur le % de gain (breakeven puis serré) ──
                if settings.get("profit_trail_enabled"):
                    if await self._check_profit_trail(position, price, change_pct, settings):
                        return

                # ── No activity sell ──────────────────────────────────
                # CORRIGÉ (en même temps que le bug ci-dessus) : ne compte "aucune
                # activité" que si DexScreener a RÉELLEMENT répondu (data non
                # vide) — sinon un token encore sur la bonding curve (jamais
                # indexé, data={}) aurait déclenché une fausse vente "no activity"
                # au bout d'un seul cycle, à tort (absence de DONNÉE, pas absence
                # RÉELLE d'activité).
                no_activity_s = settings.get("no_activity_sell_s")
                if no_activity_s and data:
                    txns_m5 = data.get("txns", {}).get("m5", {})
                    activity_count = txns_m5.get("buys", 0) + txns_m5.get("sells", 0)
                    if activity_count == 0:
                        no_activity_elapsed += effective_poll_interval
                        if no_activity_elapsed >= no_activity_s:
                            await self._close_remaining(position, price, change_pct, "NO_ACTIVITY")
                            return
                    else:
                        no_activity_elapsed = 0

                # ── Front Run Sell (best-effort, voir doc de la fonction) ─
                if settings.get("front_run_sell_enabled"):
                    triggered = await self._check_front_run_sell(position, price)
                    if triggered:
                        return

                # ── Auto-Sell on Big Buy (best-effort) ────────────────
                if settings.get("auto_sell_big_buy_levels"):
                    remaining = await self._check_big_buy_levels(position, price)
                    if remaining is not None and remaining <= 0:
                        return

                # ── Trailing SL multi-paliers (prioritaire sur les TP classiques) ─
                if settings.get("trailing_sl_enabled"):
                    if await self._check_trailing_sl(position, price, market_cap):
                        return
                else:
                    # SL classique
                    sl_pct = settings.get("sl_pct")
                    if sl_pct and change_pct <= -sl_pct:
                        await self._close_remaining(position, price, change_pct, "SL")
                        return

                    # Multi-TP
                    tp_levels = settings.get("tp_levels", [{"pct": config.TP_PCT, "sell_ratio": 1.0}])
                    for i, level in enumerate(tp_levels):
                        if i in position["tp_levels_hit"]:
                            continue
                        if change_pct >= level["pct"]:
                            await self._partial_close(position, price, change_pct, level["sell_ratio"], tp_index=i)
                            if position["units"] <= 0:
                                return

                # ── Buy The Dip (indépendant du sens de la position) ──
                dip_levels = settings.get("buy_the_dip_levels", [])
                if dip_levels:
                    await self._check_buy_the_dip(position, price)

            if position["units"] > 0:
                # CORRIGÉ (même bug que ci-dessus) : repli DexScreener-only,
                # même problème pour un token jamais migré.
                live = await get_live_price_and_market_cap(position["token_mint"])
                price = live["price"] or position["entry_price"]
                change_pct = self._pnl_pct(position, price)
                await self._close_remaining(position, price, change_pct, "EXPIRATION")
        finally:
            # AJOUTÉ : désabonnement systématique, quelle que soit la façon
            # dont la position s'est fermée (TP, SL, expiration, vente
            # manuelle...) — sans ça, les souscriptions WebSocket
            # s'accumuleraient indéfiniment sur la durée de vie du bot.
            if self.price_stream:
                await self.price_stream.unsubscribe(position["token_mint"])

    def _pnl_pct(self, position: dict, price: float) -> float:
        value = position["units"] * price
        if position["cost_basis_usd"] <= 0:
            return 0.0
        return ((value - position["cost_basis_usd"]) / position["cost_basis_usd"]) * 100

    # ══════════════════════════════════════════════════════════
    # BUY THE DIP
    # ══════════════════════════════════════════════════════════

    async def _check_buy_the_dip(self, position: dict, price: float):
        data = await _get_pair_data(position["token_mint"])
        market_cap = float(data.get("marketCap", data.get("fdv", 0)) or 0)
        if market_cap <= 0 or position["ath_market_cap"] <= 0:
            return

        drop_pct = ((position["ath_market_cap"] - market_cap) / position["ath_market_cap"]) * 100
        levels = position["settings"].get("buy_the_dip_levels", [])

        for i, level in enumerate(levels):
            if i in position["dip_levels_hit"]:
                continue
            if drop_pct >= level["drop_pct"]:
                # Conversion SOL→USD au taux en direct — voir backtest.get_sol_usd_rate
                dip_amount_usd = level["amount_sol"] * await get_sol_usd_rate()
                new_units = dip_amount_usd / price
                position["units"] += new_units
                position["cost_basis_usd"] += dip_amount_usd
                position["total_sol_invested"] = position.get("total_sol_invested", 0) + level["amount_sol"]
                position["dip_levels_hit"].append(i)
                self.data_store.save()

                log.info(
                    f"📉 [PAPER] Buy The Dip niveau {i} déclenché sur {position['token_mint'][:8]}... "
                    f"(-{drop_pct:.0f}% ATH) — achat +{level['amount_sol']} SOL"
                )
                if self.notifier:
                    await self.notifier.notify(
                        "buy_confirmed",
                        f"📉 *Buy The Dip* niveau {i+1}\nToken: `{position['token_mint'][:8]}...`\n"
                        f"Baisse: -{drop_pct:.0f}% depuis l'ATH\nAchat: +{level['amount_sol']} SOL",
                    )

    # ══════════════════════════════════════════════════════════
    # TRAILING SL MULTI-PALIERS
    # ══════════════════════════════════════════════════════════

    async def _check_trailing_sl(self, position: dict, price: float, market_cap: float) -> bool:
        tiers = sorted(position["settings"].get("trailing_sl_tiers", [{"mc_threshold": 0, "trailing_pct": 20}]),
                        key=lambda t: t["mc_threshold"])

        applicable_tier = tiers[0]
        for tier in tiers:
            if market_cap >= tier["mc_threshold"]:
                applicable_tier = tier

        if position["ath_market_cap"] <= 0:
            return False

        drawdown_pct = ((position["ath_market_cap"] - market_cap) / position["ath_market_cap"]) * 100
        if drawdown_pct >= applicable_tier["trailing_pct"]:
            change_pct = self._pnl_pct(position, price)
            await self._close_remaining(position, price, change_pct, "TRAILING_SL")
            return True
        return False

    async def _check_mc_trailing_sell(self, position: dict, price: float, market_cap: float,
                                        change_pct: float, settings: dict) -> bool:
        """
        Vente automatique si le token a dépassé mc_trailing_arm_threshold au
        moins une fois ("armé"), puis retombe à mc_trailing_sell_threshold ou
        moins. Valeurs absolues de market cap (pas des %), comme demandé.
        """
        arm_threshold = settings.get("mc_trailing_arm_threshold", 5000)
        sell_threshold = settings.get("mc_trailing_sell_threshold", 2500)

        if not position.get("mc_trailing_armed") and market_cap >= arm_threshold:
            position["mc_trailing_armed"] = True
            self.data_store.save()
            log.info(f"🔫 MC Trailing armé sur {position['token_mint'][:8]}... (MC atteint {market_cap:.0f}$)")

        if position.get("mc_trailing_armed") and market_cap <= sell_threshold:
            log.info(
                f"📉 MC Trailing déclenché sur {position['token_mint'][:8]}... "
                f"— repli à {market_cap:.0f}$ MC (seuil {sell_threshold:.0f}$)"
            )
            await self._close_remaining(position, price, change_pct, "MC_TRAILING_SELL")
            return True
        return False

    async def _check_profit_trail(self, position: dict, price: float, change_pct: float, settings: dict) -> bool:
        """
        MODIFIÉ (demande explicite, 19 août) : avant, le plancher restait
        figé à profit_trail_initial_floor_pct jusqu'à ce que le gain
        atteigne profit_trail_tight_arm_pct (2 étapes séparées — voir
        l'historique git pour l'ancienne version si besoin de comparer).
        Maintenant, dès l'armement (gain >= profit_trail_arm_pct), le
        plancher trail EN CONTINU à chaque cycle :
            plancher = max(profit_trail_initial_floor_pct, pic de gain - profit_trail_gap_pct)
        — remonté à chaque nouveau sommet de gain, jamais abaissé.
        profit_trail_tight_arm_pct n'est plus utilisé.

        Vend la totalité si le gain retombe au niveau du plancher actif.
        """
        arm_pct = settings.get("profit_trail_arm_pct", 50)
        initial_floor = settings.get("profit_trail_initial_floor_pct", 20)
        gap_pct = settings.get("profit_trail_gap_pct", 10)

        if not position.get("profit_trail_armed") and change_pct >= arm_pct:
            position["profit_trail_armed"] = True
            position["profit_trail_floor"] = initial_floor
            position["profit_trail_peak"] = change_pct
            self.data_store.save()
            log.info(f"🔒 Profit Trail armé sur {position['token_mint'][:8]}... — plancher initial +{initial_floor}%")

        if not position.get("profit_trail_armed"):
            return False

        # Suit le pic de gain observé depuis l'armement, et remonte le
        # plancher en continu à chaque nouveau sommet — jamais abaissé, et
        # jamais en dessous du plancher initial.
        peak = max(position.get("profit_trail_peak", change_pct), change_pct)
        if peak > position.get("profit_trail_peak", 0):
            position["profit_trail_peak"] = peak

        new_floor = max(initial_floor, peak - gap_pct)
        if new_floor > position.get("profit_trail_floor", initial_floor):
            position["profit_trail_floor"] = new_floor
            self.data_store.save()
            log.info(f"📈 Profit Trail remonté sur {position['token_mint'][:8]}... — plancher +{new_floor:.1f}%")

        floor = position.get("profit_trail_floor", initial_floor)
        if change_pct <= floor:
            log.info(
                f"🔒 Profit Trail déclenché sur {position['token_mint'][:8]}... "
                f"— gain retombé à {change_pct:.1f}% (plancher +{floor:.1f}%)"
            )
            await self._close_remaining(position, price, change_pct, "PROFIT_TRAIL")
            return True
        return False

    # ══════════════════════════════════════════════════════════
    # SELL INITIALS
    # ══════════════════════════════════════════════════════════

    async def sell_initials(self, position: dict, price: float) -> bool:
        """Vend exactement de quoi récupérer la mise investie (cost_basis_usd), garde le reste."""
        if position.get("initials_sold"):
            return False

        current_value = position["units"] * price
        if current_value <= position["cost_basis_usd"]:
            return False  # pas encore en profit, rien à récupérer

        units_to_sell = position["cost_basis_usd"] / price
        proceeds = units_to_sell * price  # == cost_basis_usd, par construction

        position["units"] -= units_to_sell
        recovered = position["cost_basis_usd"]
        position["cost_basis_usd"] = 0  # le reste devient un moonbag à coût nul
        position["initials_sold"] = True

        state = self.data_store.state
        state.setdefault("trades", []).append({
            "token_mint": position["token_mint"], "type": "sell_initials",
            "recovered_usd": recovered, "time": time.time(),
        })
        self.data_store.save()

        log.info(f"💰 [PAPER] Sell Initials sur {position['token_mint'][:8]}... — {recovered:.2f}$ récupérés.")
        if self.notifier:
            await self.notifier.notify(
                "sell_success",
                f"💰 *Sell Initials*\nToken: `{position['token_mint'][:8]}...`\n"
                f"Mise récupérée: {recovered:.2f}$\nLe reste est en free-ride (coût nul).",
            )
        return True

    # ══════════════════════════════════════════════════════════
    # AUTO-SELL ON BIG BUY (best-effort)
    # ══════════════════════════════════════════════════════════

    async def _check_big_buy_levels(self, position: dict, price: float):
        """
        LIMITE IMPORTANTE : détecte les gros achats en interrogeant les dernières
        transactions du pool (via Helius) et en estimant leur taille en SOL par
        différence de solde — pas un vrai flux temps réel comme F Project (qui
        lit directement les logs de validateur). Ajoute une latence de l'ordre
        du poll_interval de _monitor_position (15s par défaut), pas instantané.
        """
        pair_data = await _get_pair_data(position["token_mint"])
        pair_address = pair_data.get("pairAddress")
        if not pair_address or not config.HELIUS_API_KEY:
            return None

        try:
            big_buys = await self._fetch_recent_buy_sizes(pair_address)
        except Exception as e:
            log.debug(f"Erreur détection big buy: {e}")
            return None

        levels = position["settings"].get("auto_sell_big_buy_levels", [])
        for i, level in enumerate(levels):
            if i in position["big_buy_levels_hit"]:
                continue
            for buy_sol in big_buys:
                if level["min_sol"] <= buy_sol <= level["max_sol"]:
                    change_pct = self._pnl_pct(position, price)
                    await self._partial_close(position, price, change_pct, level["sell_ratio"], tp_index=None)
                    position["big_buy_levels_hit"].append(i)
                    self.data_store.save()
                    log.info(
                        f"🐳 [PAPER] Auto-Sell on Big Buy niveau {i} déclenché "
                        f"({buy_sol:.2f} SOL détecté) sur {position['token_mint'][:8]}..."
                    )
                    if self.notifier:
                        await self.notifier.notify(
                            "sell_success",
                            f"🐳 *Auto-Sell on Big Buy* niveau {i+1}\n"
                            f"Buy détecté: {buy_sol:.2f} SOL\nVendu: {level['sell_ratio']*100:.0f}% de la position",
                        )
                    break
        return position["units"]

    async def _fetch_recent_buy_sizes(self, pair_address: str, limit: int = 15) -> list:
        """Retourne une liste de tailles de buy estimées (en SOL) sur les dernières tx du pool."""
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": [pair_address, {"limit": limit}],
        }
        sizes = []
        signatures = await rpc_client.rpc_post(payload, timeout=10)
        if not isinstance(signatures, list):
            return sizes

        for sig_info in signatures:
            tx_payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getTransaction",
                "params": [sig_info["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
            }
            tx = await rpc_client.rpc_post(tx_payload, timeout=10)
            if not tx:
                continue
            try:
                pre = tx["meta"]["preBalances"]
                post = tx["meta"]["postBalances"]
                keys = tx["transaction"]["message"]["accountKeys"]
                keys = [k.get("pubkey", k) if isinstance(k, dict) else k for k in keys]
                idx = keys.index(pair_address) if pair_address in keys else None
                if idx is not None:
                    delta_sol = abs(post[idx] - pre[idx]) / 1_000_000_000
                    if delta_sol > 0.05:  # ignore le bruit
                        sizes.append(delta_sol)
            except (KeyError, IndexError, ValueError):
                continue
        return sizes

    # ══════════════════════════════════════════════════════════
    # FRONT RUN SELL (best-effort / BETA — voir doc)
    # ══════════════════════════════════════════════════════════

    async def _check_front_run_sell(self, position: dict, price: float) -> bool:
        """
        LIMITE IMPORTANTE : contrairement à F Project (qui vend dans le MÊME bloc
        que la consolidation détectée), cette implémentation poll toutes les
        poll_interval secondes (15s par défaut) — elle ne peut pas sortir avant
        le dump avec la même précision. C'est une approximation "beta" au sens
        propre du terme, à ne pas considérer comme un vrai edge de vitesse.
        """
        group = self.data_store.get_linked_group_for_wallet(position["source_wallet"])
        if len(group) < 2 or not config.HELIUS_API_KEY:
            return False

        try:
            balances = await self._fetch_token_balances(group, position["token_mint"])
        except Exception as e:
            log.debug(f"Erreur Front Run Sell: {e}")
            return False

        total = sum(balances.values())
        if total <= 0:
            return False

        max_holder_pct = max(balances.values()) / total * 100
        threshold = position["settings"].get("front_run_sell_threshold_pct", 50)

        if max_holder_pct >= threshold:
            change_pct = self._pnl_pct(position, price)
            await self._close_remaining(position, price, change_pct, "FRONT_RUN_SELL")
            log.info(f"⚡ [PAPER] Front Run Sell déclenché — consolidation {max_holder_pct:.0f}% détectée.")
            if self.notifier:
                await self.notifier.notify(
                    "rugger_alert",
                    f"⚡ *Front Run Sell* déclenché (beta)\nConsolidation détectée: {max_holder_pct:.0f}%\n"
                    f"Position vendue.",
                )
            return True
        return False

    async def _fetch_token_balances(self, wallets: list, token_mint: str) -> dict:
        balances = {}
        for w in wallets:
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [w, {"mint": token_mint}, {"encoding": "jsonParsed"}],
            }
            try:
                result = await rpc_client.rpc_post(payload, timeout=10)
                accounts = result.get("value", []) if isinstance(result, dict) else []
                total = 0.0
                for acc in accounts:
                    amount = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"]
                    total += amount or 0
                balances[w] = total
            except Exception:
                balances[w] = 0.0
        return balances

    async def _check_dev_behavior(self, dev_address: str, token_mint: str, entry_price: float) -> dict:
        """
        Filtres "Achat min/max du développeur" et "Détention dev %" (comme F Project).

        LIMITE : approxime l'achat du dev en SOL via (tokens détenus × prix actuel ÷
        SOL_USD_RATE) — ce n'est PAS son coût d'achat réel (qui aurait été à un prix
        différent, souvent plus bas) mais la valeur actuelle de sa position. Pour un
        token qui vient d'être créé et n'a pas encore bougé, l'écart est faible ;
        il grandit si le prix a déjà varié entre la création et cette vérification.

        Retourne : {"dev_buy_sol": float, "dev_holding_pct": float} ou None si
        les données n'ont pas pu être récupérées (le filtre est alors ignoré,
        jamais bloquant par manque de données).
        """
        balances = await self._fetch_token_balances([dev_address], token_mint)
        dev_tokens = balances.get(dev_address, 0.0)
        if dev_tokens <= 0:
            return None

        supply_payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenSupply",
            "params": [token_mint],
        }
        supply_result = await rpc_client.rpc_post(supply_payload, timeout=10)
        try:
            total_supply = float(supply_result["value"]["uiAmount"])
        except (KeyError, TypeError, ValueError):
            return None
        if total_supply <= 0:
            return None

        dev_value_usd = dev_tokens * entry_price
        dev_buy_sol = dev_value_usd / await get_sol_usd_rate()
        dev_holding_pct = (dev_tokens / total_supply) * 100

        return {"dev_buy_sol": dev_buy_sol, "dev_holding_pct": dev_holding_pct}

    # ══════════════════════════════════════════════════════════
    # FERMETURE DE POSITION (partielle / totale)
    # ══════════════════════════════════════════════════════════

    async def _partial_close(self, position: dict, exit_price: float, change_pct: float,
                              sell_ratio: float, tp_index):
        units_sold = position["units"] * sell_ratio

        sell_result = await self._execute_sell(position["token_mint"], units_sold, exit_price, position["settings"])
        if sell_result is None:
            log.error(f"❌ Vente partielle échouée sur {position['token_mint'][:8]}... — position inchangée, nouvel essai au prochain poll.")
            if self.notifier:
                await self.notifier.notify(
                    "sell_failed",
                    f"❌ *Sell Failed*\nToken: `{position['token_mint'][:8]}...`\nVente partielle échouée, nouvel essai automatique.",
                )
            return

        proceeds = sell_result["proceeds_usd"]
        sell_signature = sell_result.get("signature")
        cost_removed = position["cost_basis_usd"] * sell_ratio
        pnl_usd = proceeds - cost_removed

        position["units"] -= units_sold
        position["cost_basis_usd"] -= cost_removed
        position["total_sol_received"] = position.get("total_sol_received", 0) + (proceeds / await get_sol_usd_rate())
        if sell_signature:
            position.setdefault("tx_signatures", []).append(sell_signature)
        if tp_index is not None:
            position["tp_levels_hit"].append(tp_index)

        state = self.data_store.state
        state["total_pnl_usd"] = state.get("total_pnl_usd", 0) + pnl_usd
        state.setdefault("trades", []).append({
            "token_mint": position["token_mint"], "type": "partial_close",
            "sell_ratio": sell_ratio, "result_pct": change_pct, "pnl_usd": pnl_usd, "time": time.time(),
        })
        self.data_store.save()

        log.info(
            f"✅ [{config.TRADING_MODE}] Vente partielle sur {position['token_mint']} "
            f"({change_pct:+.1f}%) — vendu {sell_ratio*100:.0f}% de la position ({pnl_usd:+.2f}$)"
        )

        if position["units"] <= 0.0000001:
            position["status"] = "closed"
            state["open_positions"] = [p for p in state.get("open_positions", []) if p is not position]
            state.setdefault("closed_positions", []).append(position)
            self.data_store.save()

    async def _close_remaining(self, position: dict, exit_price: float, change_pct: float, close_reason: str):
        if position["units"] <= 0:
            return

        sell_result = await self._execute_sell(position["token_mint"], position["units"], exit_price, position["settings"])
        if sell_result is None:
            log.error(f"❌ Clôture échouée sur {position['token_mint'][:8]}... ({close_reason}) — position reste ouverte, nouvel essai au prochain poll.")
            if self.notifier:
                await self.notifier.notify(
                    "sell_failed",
                    f"❌ *Sell Failed* ({close_reason})\nToken: `{position['token_mint'][:8]}...`\nVente échouée, nouvel essai automatique.",
                )
            return

        proceeds = sell_result["proceeds_usd"]
        sell_signature = sell_result.get("signature")
        pnl_usd = proceeds - position["cost_basis_usd"]

        position["status"] = "closed"
        position["exit_price"] = exit_price
        position["exit_time"] = time.time()
        position["result_pct"] = change_pct
        position["close_reason"] = close_reason
        position["total_sol_received"] = position.get("total_sol_received", 0) + (proceeds / await get_sol_usd_rate())
        position["units"] = 0
        position["cost_basis_usd"] = 0
        if sell_signature:
            position.setdefault("tx_signatures", []).append(sell_signature)

        state = self.data_store.state
        state["open_positions"] = [p for p in state.get("open_positions", []) if p is not position]
        state.setdefault("closed_positions", []).append(position)
        state["total_pnl_usd"] = state.get("total_pnl_usd", 0) + pnl_usd

        if pnl_usd < 0:
            state["session_losses_usd"] = state.get("session_losses_usd", 0) + abs(pnl_usd)

        self.data_store.save()

        just_paused = self.data_store.record_trade_result(position["source_wallet"], is_win=(pnl_usd >= 0))

        emoji = "✅" if pnl_usd >= 0 else "❌"
        log.info(
            f"{emoji} [{config.TRADING_MODE}] Position clôturée ({close_reason}) sur {position['token_mint']} "
            f"— {change_pct:+.1f}% ({pnl_usd:+.2f}$)"
        )

        # ── Détection "repérage" ────────────────────────────────────────
        # Une perte qui arrive anormalement vite est un signal possible que
        # le rugger a changé de comportement ou nous a repérés (cf. vidéos :
        # "si le mec vend juste après toi, supprime son adresse"). Alerte
        # seulement — aucune action automatique, la décision reste manuelle.
        time_in_position_s = position["exit_time"] - position["entry_time"]
        possible_detection = (
            pnl_usd < 0
            and time_in_position_s < config.DETECTION_ALERT_TIME_THRESHOLD_S
            and close_reason in ("SL", "NO_ACTIVITY")
        )

        if self.notifier:
            sig_line = f"\nTx: `{sell_signature[:16]}...`" if sell_signature else ""
            await self.notifier.notify(
                "sell_success",
                f"{emoji} *Sell Success* ({close_reason})\nToken: `{position['token_mint'][:8]}...`\n"
                f"Résultat: {change_pct:+.1f}% ({pnl_usd:+.2f}$){sig_line}",
            )

            if possible_detection:
                try:
                    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(
                        "🗑 Supprimer ce rugger", callback_data=f"askdelete_{position['source_wallet']}",
                    )]])
                except ImportError:
                    keyboard = None
                await self.notifier.notify(
                    "rugger_alert",
                    f"⚠️ *Possible repérage détecté*\nToken: `{position['token_mint'][:8]}...`\n"
                    f"Perte de {change_pct:.1f}% en seulement {time_in_position_s:.0f}s — le pattern habituel "
                    f"de ce wallet a peut-être changé, ou il t'a repéré.\n"
                    f"_Vérifie manuellement avant de continuer à le suivre._",
                    reply_markup=keyboard,
                )

            # Carte PNL visuelle (comme F Project), en complément du message texte
            #
            # CORRIGÉ suite à un vrai signalement ("pourquoi tous les tokens
            # ont le même nom ?") : token_label prenait TOUJOURS l'adresse du
            # mint tronquée, jamais le vrai symbole (BAMBI, PIMBA...) — d'où
            # l'impression que chaque carte se ressemblait. Tente maintenant
            # de récupérer le vrai symbole via DexScreener (appel léger, pas
            # de décodage RPC lourd) ; repli sur l'adresse tronquée si le
            # token n'est pas encore indexé (fréquent pour un token encore en
            # bonding curve au moment de la fermeture — DexScreener n'indexe
            # que les tokens migrés, voir les nombreuses notes plus haut dans
            # ce fichier sur cette même limite).
            token_label = position["token_mint"][:8] + "..."
            try:
                pair_data = await _get_pair_data(position["token_mint"])
                symbol = (pair_data or {}).get("baseToken", {}).get("symbol")
                if symbol:
                    token_label = symbol
            except Exception:
                pass  # repli silencieux sur l'adresse tronquée — jamais bloquant pour l'envoi de la carte

            try:
                from pnl_card import build_pnl_card
                pnl_sol = position.get("total_sol_received", 0) - position.get("total_sol_invested", 0)
                card_bytes = build_pnl_card(
                    token_label=token_label,
                    result_pct=change_pct,
                    bought_sol=position.get("total_sol_invested", 0),
                    sold_sol=position.get("total_sol_received", 0),
                    holding_sol=0.0,
                    pnl_sol=pnl_sol,
                    pnl_usd=pnl_usd,
                    reason=close_reason,
                )
                await self.notifier.notify_photo("sell_success", card_bytes, caption="")
            except Exception as e:
                log.debug(f"Erreur génération/envoi carte PNL: {e}")

            if just_paused:
                await self.notifier.notify(
                    "rugger_alert",
                    f"🛑 *Max Loss Counter atteint* — auto-buy en pause\n"
                    f"Wallet: `{position['source_wallet'][:8]}...`\n"
                    f"Réactive-le manuellement depuis le menu Ruggers.",
                )
