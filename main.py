"""
════════════════════════════════════════════════════════════════
MAIN — Orchestrateur du sniper bot
════════════════════════════════════════════════════════════════
Boucle complète :
1. Écoute les nouveaux tokens Pump.fun (detection/websocket_listener)
2. Si le dev est déjà dans le monitoring → achat simulé immédiat
3. Si le dev est nouveau → trace ses fonds (tracing/fund_tracer),
   regarde son historique (analysis/wallet_history), backteste
   (analysis/backtest), et l'ajoute au monitoring si rentable
4. Toutes les décisions et positions sont journalisées dans data.json

Mode PAPER par défaut — voir config.py.
"""

import asyncio
import logging
import os
import sys

import config
from websocket_listener import NewTokenListener
from copytrade_listener import CopyTradeListener
import fund_tracer
import wallet_history
import backtest
import ai_advisor
from monitoring_list import DataStore
from paper_trader import PaperTrader
from live_trader import LiveTrader
from protection_scanner import ProtectionScanner
from rug_scanner import RugScanner
from wallet_cleanup import WalletCleanup
from notifier import Notifier
from telegram_bot import SniperTelegramBot
from position_price_stream import PositionPriceStream
import wallet

# CORRIGÉ suite à un vrai crash en boucle signalé : logging.FileHandler()
# ne crée JAMAIS le dossier parent de LOG_FILE tout seul — si config.LOG_FILE
# pointe vers un chemin de volume (ex: /app/data/sniper_bot.log) et que ce
# dossier n'existe pas encore au démarrage du conteneur, FileHandler lève
# une FileNotFoundError qui plante le bot AVANT même que le code métier ne
# démarre. railway.json relance alors le conteneur (restartPolicyMaxRetries),
# qui replante exactement pareil à chaque fois — boucle infinie de crash.
_log_dir = os.path.dirname(config.LOG_FILE)
if _log_dir:
    os.makedirs(_log_dir, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
    ],
)
# Désactive les logs bruyants et peu utiles des bibliothèques internes
# (httpx affiche CHAQUE requête HTTP du polling Telegram — getUpdates
# toutes les 10s, sendMessage, editMessageText, etc. — jamais utile à
# lire, ça noie les vrais logs du bot dedans).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)

log = logging.getLogger("main")


class SniperBot:
    def __init__(self):
        self.data_store = DataStore()
        self.notifier = Notifier(self.data_store)
        self.position_price_stream = PositionPriceStream()

        if config.TRADING_MODE == "LIVE":
            self.trader = LiveTrader(self.data_store, notifier=self.notifier, price_stream=self.position_price_stream)
        else:
            self.trader = PaperTrader(self.data_store, notifier=self.notifier, price_stream=self.position_price_stream)

        self.listener = NewTokenListener(on_new_token=self.on_new_token, data_store=self.data_store)
        self.copytrade_listener = CopyTradeListener(
            self.data_store, on_buy=self.on_copytrade_buy, on_sell=self.on_copytrade_sell,
            on_large_sol_transfer=self.on_dev_large_sol_transfer,
            on_withdrawal=self.on_wallet_withdrawal,
        )
        self.protection_scanner = ProtectionScanner(self.data_store, notifier=self.notifier)
        self.rug_scanner = RugScanner(self.data_store, notifier=self.notifier)
        self.wallet_cleanup = WalletCleanup(self.data_store, notifier=self.notifier)
        self.telegram_bot = SniperTelegramBot(self.data_store, self.notifier, self.trader)

    async def run(self):
        log.info(f"🚀 Sniper Bot démarré — mode : {config.TRADING_MODE}")
        if not config.HELIUS_API_KEY:
            log.error(
                "❌ Aucune clé HELIUS_API_KEY configurée. Le bot ne peut pas fonctionner "
                "correctement sans accès WebSocket premium. Voir README.md."
            )
            return

        if config.TRADING_MODE == "LIVE":
            if not config.SOLANA_PRIVATE_KEY:
                log.error("❌ TRADING_MODE=LIVE mais SOLANA_PRIVATE_KEY est vide. Arrêt.")
                return
            try:
                pubkey = wallet.get_public_key()
                balance = await wallet.get_sol_balance()
                log.info(f"💰 Wallet LIVE : {pubkey} — solde actuel : {balance:.4f} SOL")
                if balance <= 0:
                    log.error("❌ Solde SOL nul ou introuvable — arrêt par sécurité, aucun ordre ne sera passé.")
                    return
            except Exception as e:
                log.error(f"❌ Impossible de charger le wallet LIVE : {e}. Arrêt.")
                return

        # Scan permanent Pump.fun (détection + évaluation) — désactivable
        # temporairement via config.DETECTION_ENABLED (voir config.py).
        # Le reste (Telegram, Protection, Copy Trading) continue de tourner.
        tasks = [self.protection_scanner.start(), self.copytrade_listener.start(), self.rug_scanner.start(),
                 self.wallet_cleanup.start(), self.position_price_stream.start()]
        if config.DETECTION_ENABLED:
            tasks.append(self.listener.start())
        else:
            log.warning("⏸️  Scan permanent Pump.fun DÉSACTIVÉ (config.DETECTION_ENABLED=false) — "
                        "aucun nouveau dev ne sera découvert automatiquement pour l'instant.")

        if config.BOT_TOKEN:
            await self.telegram_bot.run_polling()
        else:
            log.warning("⚠️  Aucun BOT_TOKEN configuré — le bot Telegram ne démarre pas (notifications désactivées).")

        # Le scanner de protection tourne en parallèle du listener WebSocket :
        # il ajoute proactivement les wallets financés par les adresses mère/exchange
        # surveillées, AVANT qu'ils créent un token — c'est ce qui permet le "bloc zéro"
        # au lieu d'évaluer réactivement après coup (voir execution/protection_scanner.py).
        await asyncio.gather(*tasks)

    async def on_new_token(self, info: dict):
        """Callback appelé pour chaque nouveau token Pump.fun détecté."""
        token_mint = info["token_mint"]
        dev_address = info["dev_address"]
        creation_slot = info.get("creation_slot")

        log.info(f"🆕 Nouveau token {token_mint[:8]}... par dev {dev_address[:8]}...")

        # AJOUTÉ avec le scanner de rugs complet (rug_scanner.py) — enregistre
        # CE token pour un suivi indépendant, peu importe s'il est déjà géré
        # ci-dessous par le pipeline d'évaluation existant. Ne fait rien si
        # RUG_SCAN_ENABLED=false (voir la garde dans register_candidate()).
        self.rug_scanner.register_candidate(token_mint, dev_address, created_at=info.get("created_at"))

        # Cas 1 : ce dev est déjà validé et surveillé.
        if self.data_store.is_dev_monitored(dev_address):
            wallet_info = self.data_store.state["monitored_dev_wallets"][dev_address]
            mode = wallet_info.get("mode", "track_creation")

            # AJOUTÉ pour le nettoyage automatique (wallet_cleanup.py) :
            # toute activité connue repousse le délai d'inactivité, peu
            # importe le mode.
            self.data_store.update_wallet_activity(dev_address)

            if mode == "track_creation":
                await self.trader.open_position(
                    token_mint, dev_address,
                    reason=f"Dev connu ({wallet_info['label']}, ratio backtest {wallet_info['backtest_ratio']:.2f})",
                    creation_slot=creation_slot,
                )
                return

            if mode == "buy_on_dev_sell":
                # On n'achète PAS à la création — on attend que le dev vende
                # son propre token (bougie rouge) pour racheter juste après,
                # potentiellement à un meilleur point d'entrée que le bloc zéro.
                self.data_store.set_pending_dev_sell_watch(dev_address, token_mint)
                log.info(
                    f"👁️  Buy on Dev Sell : surveillance de {dev_address[:8]}... sur "
                    f"{token_mint[:8]}... (achat dès que le dev vend, timeout "
                    f"{config.BUY_ON_DEV_SELL_TIMEOUT_MIN}min)"
                )
                return

            # track_buy / track_sell : géré par copytrade_listener, pas ici.
            return

        # AJOUTÉ (demande explicite, 19 août) : si auto_add_new_devs=False,
        # n'évalue même pas ce nouveau dev — achète UNIQUEMENT les créations
        # des Ruggeurs déjà dans la liste (Cas 1 ci-dessus, jamais affecté).
        # Distinct de filters_enabled=False (qui fait l'inverse : accepte
        # tout le monde). Voir config.DEFAULT_AUTO_DETECTION_SETTINGS.
        det_settings = self.data_store.state.get("auto_detection_settings", dict(config.DEFAULT_AUTO_DETECTION_SETTINGS))
        if not det_settings.get("auto_add_new_devs", True):
            return

        # Cas 2 : nouveau dev → pipeline complet d'évaluation
        #
        # CORRIGÉ suite à un vrai bug trouvé (déconnexions WebSocket
        # systématiques observées en conditions réelles, à chaque évaluation
        # sans exception) : _evaluate_new_dev peut prendre jusqu'à 70+
        # secondes (backtest sur plusieurs tokens, chacun décodant jusqu'à 40
        # transactions). Comme on_new_token était directement awaité DANS la
        # boucle "async for message in ws" de websocket_listener.py, toute
        # cette durée bloquait la réception du message suivant ET le
        # mécanisme de ping/pong interne de la connexion — provoquant un
        # timeout de keepalive et une reconnexion forcée à chaque fois.
        # asyncio.create_task() lance maintenant l'évaluation EN PARALLÈLE :
        # on_new_token (et donc la boucle WebSocket) revient immédiatement,
        # libre de traiter le message suivant et de répondre aux pings, pendant
        # que l'évaluation continue en arrière-plan. _run_evaluation_task
        # capture les exceptions pour qu'elles restent visibles dans les logs
        # (une tâche asyncio non attendue les avale sinon silencieusement).
        asyncio.create_task(self._run_evaluation_task(dev_address, token_mint))

    async def _run_evaluation_task(self, dev_address: str, token_mint: str):
        try:
            await self._evaluate_new_dev(dev_address, token_mint)
        except Exception as e:
            log.error(f"❌ Erreur pendant l'évaluation en arrière-plan de {dev_address[:8]}...: {e}")

    async def on_copytrade_buy(self, wallet_address: str, token_mint: str, signature: str,
                                source_sol_spent_lamports: int = 0, source_tokens_received: float = 0):
        """
        Callback du copytrade_listener : un wallet suivi en mode track_buy vient
        d'acheter un token. Applique Max Token Age et Follow Cooldown (les deux
        étaient configurables depuis longtemps mais jamais vérifiés avant cette
        version), puis ouvre une position miroir.

        source_sol_spent_lamports/source_tokens_received : AJOUTÉS suite à une
        demande explicite ("pourquoi la notif ne montre pas à quel market cap
        MON adresse suivie est rentrée ?") — permet de calculer le market cap
        d'entrée du wallet SOURCE lui-même (pas seulement le nôtre), affiché
        dans "Buy Confirmed" à côté du nôtre pour comparaison directe. Voir
        copytrade_listener._classify_transaction pour l'extraction.
        """
        entry = self.data_store.state["monitored_dev_wallets"].get(wallet_address)
        if not entry or entry.get("mode") != "track_buy":
            return

        # AJOUTÉ pour le nettoyage automatique — voir on_new_token pour le
        # même ajout côté track_creation.
        self.data_store.update_wallet_activity(wallet_address)

        settings = entry["settings"]
        label = entry.get("label", wallet_address[:8] + "...")

        # Follow Cooldown : ignore les achats trop rapprochés du même wallet
        if not self.data_store.check_and_update_follow_cooldown(wallet_address):
            log.info(f"⏭️  Copy trade ignoré ({label}) — Follow Cooldown actif.")
            return

        # Max Token Age : ignore les tokens trop vieux au moment où le wallet achète
        max_age = settings.get("max_token_age_min", 0)
        if max_age:
            age = await backtest.get_token_age_minutes(token_mint)
            if age is not None and age > max_age:
                log.info(f"⏭️  Copy trade ignoré ({label}) — token âgé de {age:.1f}min > max {max_age}min.")
                return

        # CORRIGÉ (demande explicite, 19 août — latence) : ces deux vérifications
        # faisaient chacune leur propre recherche du créateur du token, en
        # SÉQUENCE (deux allers-retours RPC l'un après l'autre) — alors
        # qu'elles ont besoin de la MÊME information. wallet_created_this_token
        # ne vérifiait en plus QUE la transaction d'achat elle-même (ratait le
        # cas où le dev crée le token puis l'achète dans une transaction
        # SÉPARÉE plus tard) — get_token_creator, plus robuste (remonte à la
        # vraie transaction de création du mint), couvre maintenant les deux
        # usages en un seul appel.
        token_creator = await wallet_history.get_token_creator(token_mint)

        # AJOUTÉ (demande explicite) : n'achète PAS quand ce wallet Copy
        # Trading est LUI-MÊME le créateur du token — schéma create+buy très
        # courant chez les devs. Copier ça reviendrait à sniper toutes ses
        # créations depuis un wallet en track_buy — c'est précisément le
        # rôle de track_creation (Ruggeur), pas de Copy Trading.
        if token_creator == wallet_address:
            log.info(f"⏭️  Copy trade ignoré ({label}) — c'est sa propre création (Copy Trading ne sniper pas les créations).")
            return

        # AJOUTÉ (demande explicite — individuel par wallet) : n'achète pas si
        # le token a été créé par un dev sur la liste des devs bloqués DE CE
        # WALLET précis — chaque wallet Copy Trading a sa propre liste,
        # indépendante des autres. Voir monitoring_list.is_dev_blocked.
        if token_creator and self.data_store.is_dev_blocked(wallet_address, token_creator):
            log.info(f"⏭️  Copy trade ignoré ({label}) — token créé par un dev bloqué sur ce wallet ({token_creator[:8]}...).")
            return

        # AJOUTÉ (demande explicite) : market cap d'entrée du wallet SOURCE,
        # calculé avec la MÊME méthode (prix on-chain × taux SOL/USD en
        # direct) que celle déjà utilisée pour notre propre entrée — pas une
        # approximation différente qui donnerait des chiffres incohérents
        # entre les deux. None si les montants n'ont pas pu être extraits de
        # la transaction (cas limite, ne bloque jamais l'achat).
        source_entry_market_cap = None
        if source_sol_spent_lamports and source_tokens_received:
            try:
                source_price_sol = (source_sol_spent_lamports / 1_000_000_000) / (source_tokens_received / 1_000_000)
                sol_rate = await backtest.get_sol_usd_rate()
                source_entry_market_cap = source_price_sol * backtest.PUMPFUN_STANDARD_TOTAL_SUPPLY * sol_rate
            except (ZeroDivisionError, TypeError, ValueError):
                source_entry_market_cap = None
        else:
            # AJOUTÉ (demande explicite, 19 août) : diagnostic pour un vrai
            # cas signalé où "Market cap (entrée wallet suivi)" n'apparaît
            # pas dans la notification — l'extraction échoue probablement
            # pour un schéma de swap plus complexe (routage multi-hop via un
            # agrégateur type Jupiter, où le mouvement SOL du wallet source
            # n'est pas directement adjacent au tokenTransfer dans la
            # transaction — voir _classify_transaction). Ce log précise
            # LEQUEL des deux montants manque, pour distinguer ce cas d'une
            # éventuelle vraie régression.
            log.info(
                f"ℹ️ Market cap source non calculable pour {label} sur {token_mint[:8]}... "
                f"(sol_spent={source_sol_spent_lamports}, tokens_received={source_tokens_received}) "
                f"— probablement un swap routé via plusieurs sauts, pas une erreur bloquante."
            )

        log.info(f"📋 Copy trade détecté : {label} a acheté {token_mint[:8]}... (tx {signature[:12]}...)")
        await self.trader.open_position(
            token_mint, wallet_address,
            reason=f"Copy trade — achat détecté chez {label}",
            source_entry_market_cap=source_entry_market_cap,
        )

    async def on_wallet_withdrawal(self, wallet_address: str, destination: str, amount: float, asset: str = "SOL", signature: str = None):
        """
        Callback du copytrade_listener : un wallet surveillé (Ruggeur OU
        Copy Trading, sans distinction) vient d'envoyer du SOL OU DE
        L'USDC vers une autre adresse, au-delà du montant minimum configuré
        pour cet actif.

        MODIFIÉ suite à une demande explicite : ajoute maintenant AUSSI
        l'adresse destinataire au monitoring, comme le fait déjà
        on_dev_large_sol_transfer (seuil %). Contrairement à ce dernier,
        le seuil ici est bas et générique (0.1 SOL par défaut) — donc
        chaque petit retrait déclenchera un ajout, ce qui peut remplir la
        liste de 30 wallets plus vite avec du bruit (exchanges, transferts
        personnels...). Assumé suite à la demande explicite, mais à
        surveiller : réduire WITHDRAWAL_ALERT_MIN_SOL trop bas multipliera
        les ajouts automatiques.

        AJOUTÉ (2e fois) suite à une demande explicite : bouton "Voir sur
        Solscan" pointant directement vers la transaction concernée.

        ÉTENDU (3e fois) suite à une demande explicite : accepte maintenant
        aussi les retraits USDC (paramètre "asset"), et affiche la valeur
        totale du wallet (SOL + USDC, voir wallet.get_wallet_value_summary)
        en plus du solde restant dans l'actif transféré.
        """
        entry = self.data_store.state["monitored_dev_wallets"].get(wallet_address, {})
        label = entry.get("label", wallet_address[:8] + "...")
        unit = "SOL" if asset == "SOL" else "USDC"

        # AJOUTÉ suite à une demande explicite : précise si le wallet SOURCE
        # (celui qui retire) est suivi en mode Ruggeur (dev-sniping) ou Copy
        # Trading — même catégorisation que count_wallets_by_mode (utilisée
        # pour le compteur du menu principal), pour rester cohérent partout.
        source_mode = entry.get("mode", "track_creation")
        if source_mode in ("track_creation", "buy_on_dev_sell"):
            type_label = "🎯 Ruggeur (dev)"
        elif source_mode in ("track_buy", "track_sell"):
            type_label = "📋 Copy Trading"
        else:
            type_label = f"❔ mode inconnu ({source_mode})"

        log.info(f"📤 Retrait {unit} : {label} a envoyé {amount:.4f} {unit} vers {destination[:8]}...")

        # AJOUTÉ suite à une demande explicite : affiche le solde restant
        # DANS L'ACTIF TRANSFÉRÉ, PLUS la valeur totale du wallet (SOL +
        # USDC combinés) — lus en direct via RPC, pas recalculés à partir
        # du montant transféré, plus fiable si d'autres mouvements ont eu
        # lieu entre-temps.
        try:
            summary = await wallet.get_wallet_value_summary(wallet_address)
            if asset == "SOL":
                balance_line = f"Solde SOL restant : `{summary['sol_balance']:.4f}` SOL\n"
            else:
                balance_line = f"Solde USDC restant : `{summary['usdc_balance']:.2f}` USDC\n"
            balance_line += f"Valeur totale du wallet (SOL + USDC) : `${summary['total_value_usd']:,.2f}`\n"
        except Exception as e:
            log.debug(f"Erreur lecture valeur totale pour {wallet_address}: {e}")
            balance_line = ""

        # CORRIGÉ suite à un vrai effet en cascade observé : une adresse
        # ajoutée automatiquement par CETTE alerte pouvait elle-même
        # déclencher un nouvel ajout automatique si elle faisait aussi un
        # retrait — sans limite. Deux garde-fous, tous deux réglables
        # depuis Telegram (menu "📤 Alerte retrait SOL") :
        # 1. "auto_add" : coupe l'ajout automatique entièrement (garde
        #    juste la notification) si désactivé.
        # 2. "allow_cascade" : si désactivé (par défaut), une adresse dont
        #    le schéma est déjà "sol_withdrawal"/"sol_transfer" (donc
        #    elle-même ajoutée automatiquement par une alerte précédente)
        #    ne peut plus déclencher de NOUVEL ajout — la chaîne s'arrête
        #    au 1er niveau.
        wd_settings = self.data_store.get_withdrawal_alert_settings()
        was_auto_added = entry.get("scheme") in ("sol_withdrawal", "sol_transfer")
        cascade_blocked = was_auto_added and not wd_settings.get("allow_cascade", False)
        # AJOUTÉ suite à un vrai cas observé : un wallet source très actif
        # (bot/service) déclenchait plusieurs ajouts DIFFÉRENTS en quelques
        # secondes — pas couvert par le blocage de cascade ci-dessus,
        # puisque ce n'est pas une adresse auto-ajoutée qui recascade,
        # c'est la source d'origine qui spamme. Cooldown appliqué
        # SEULEMENT si on s'apprête réellement à ajouter (pas la peine de
        # consommer le cooldown pour rien si auto_add est déjà désactivé
        # ou la cascade déjà bloquée).
        cooldown_blocked = False
        if wd_settings.get("auto_add", True) and not cascade_blocked:
            cooldown_blocked = not self.data_store.check_and_update_auto_add_cooldown(wallet_address)

        added_note = ""
        # AJOUTÉ suite à une demande explicite (19 août) : une adresse de
        # retrait issue d'un wallet Copy Trading n'est PLUS jamais ajoutée au
        # monitoring, ni visible ni en arrière-plan — contrairement à un
        # Ruggeur (dev), où le comportement précédent (arrière-plan +
        # cascade/cooldown) reste inchangé, juste masqué du menu (voir plus
        # bas, linked_to_parent).
        if source_mode in ("track_buy", "track_sell"):
            added_note = "\n\n⏭️ _Adresse destinataire NON ajoutée (source Copy Trading — pas de suivi des retraits pour ce type)._"
        elif not wd_settings.get("auto_add", True):
            added_note = ""  # ajout auto désactivé — juste la notification, rien à signaler de plus
        elif cascade_blocked:
            added_note = "\n\n⏭️ _Adresse destinataire NON ajoutée (cascade bloquée — ce wallet a lui-même été ajouté automatiquement)._"
        elif cooldown_blocked:
            added_note = "\n\n⏳ _Adresse destinataire NON ajoutée (cooldown — ce wallet source a déjà déclenché un ajout automatique récemment)._"
        elif not self.data_store.is_dev_monitored(destination) and self.data_store.has_free_slot():
            # CORRIGÉ suite à un vrai bug trouvé : add_dev_wallet() ajoutait
            # TOUJOURS le destinataire en mode "track_creation" (sa valeur
            # par défaut), sans jamais regarder le mode réel de la source —
            # un destinataire d'un wallet en Copy Trading (track_buy)
            # atterrissait quand même dans les Ruggeurs, sans aucun lien
            # logique. Hérite maintenant du mode de la source. (Le cas Copy
            # Trading lui-même est maintenant exclu plus haut avant même
            # d'arriver ici — cette branche ne concerne donc que les
            # Ruggeurs en pratique.)
            inherited_mode = entry.get("mode", "track_creation")
            self.data_store.add_dev_wallet(
                destination, label=f"retrait_{wallet_address[:6]}_{destination[:6]}", scheme="sol_withdrawal",
                backtest_ratio=0.0, mode=inherited_mode,
                # AJOUTÉ suite à une demande explicite : rattachée en
                # arrière-plan au parent plutôt que visible comme entrée
                # séparée — voir monitoring_list.add_dev_wallet et
                # show_ruggers_menu (compteur affiché sur la ligne du parent).
                linked_to_parent=wallet_address,
            )
            added_note = "\n\n_Adresse destinataire ajoutée au monitoring (en arrière-plan, rattachée à ce wallet)._"
        elif not self.data_store.has_free_slot():
            added_note = "\n\n⚠️ _Limite de wallets atteinte — adresse destinataire NON ajoutée._"

        reply_markup = None
        if signature:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔍 Voir sur Solscan", url=f"https://solscan.io/tx/{signature}")
            ]])

        await self.notifier.notify(
            "withdrawal_alert",
            f"📤 *Retrait {unit} détecté*\n\n"
            f"Wallet : `{wallet_address}` ({label})\n"
            f"Type : {type_label}\n"
            f"Montant : `{amount:.4f}` {unit}\n"
            f"{balance_line}"
            f"Destination : `{destination}`{added_note}",
            reply_markup=reply_markup,
        )

    async def on_dev_large_sol_transfer(self, dev_address: str, destination: str, amount_sol: float, pct_of_balance: float, signature: str = None):
        """
        Callback du copytrade_listener : un dev surveillé (mode
        track_creation) vient de transférer une grosse partie de son solde
        SOL vers une autre adresse — signal fort qu'il encaisse et se
        prépare à disparaître. AJOUTÉ suite à une demande explicite.

        Deux actions : (1) alerte Telegram immédiate, (2) ajout automatique
        de l'adresse DESTINATAIRE au monitoring — elle pourrait être un
        autre wallet contrôlé par la même personne, ou une piste utile pour
        la méthode "adresse intermédiaire".

        AJOUTÉ (2e fois) suite à une demande explicite : bouton "Voir sur
        Solscan" pointant directement vers la transaction concernée.
        """
        entry = self.data_store.state["monitored_dev_wallets"].get(dev_address, {})
        label = entry.get("label", dev_address[:8] + "...")

        log.warning(
            f"💸 ALERTE : {label} a transféré {amount_sol:.4f} SOL ({pct_of_balance:.0f}% de son solde) "
            f"vers {destination[:8]}..."
        )

        # CORRIGÉ suite à un vrai effet en cascade observé (même correctif
        # que on_wallet_withdrawal) : évite qu'une adresse déjà ajoutée
        # automatiquement (scheme sol_transfer/sol_withdrawal) ne déclenche
        # elle-même un nouvel ajout en chaîne. Corrige aussi une
        # incohérence : le texte de la notification annonçait l'ajout
        # AVANT même de vérifier s'il avait réussi.
        dt_settings = self.data_store.get_dev_transfer_settings()
        was_auto_added = entry.get("scheme") in ("sol_withdrawal", "sol_transfer")
        cascade_blocked = was_auto_added and not dt_settings.get("allow_cascade", False)
        # AJOUTÉ — même cooldown par wallet source que on_wallet_withdrawal.
        cooldown_blocked = False
        if dt_settings.get("auto_add", True) and not cascade_blocked:
            cooldown_blocked = not self.data_store.check_and_update_auto_add_cooldown(dev_address)

        added_note = ""
        if not dt_settings.get("auto_add", True):
            added_note = ""
        elif cascade_blocked:
            added_note = "\n\n⏭️ _Adresse destinataire NON ajoutée (cascade bloquée — ce wallet a lui-même été ajouté automatiquement)._"
        elif cooldown_blocked:
            added_note = "\n\n⏳ _Adresse destinataire NON ajoutée (cooldown — ce wallet source a déjà déclenché un ajout automatique récemment)._"
        elif not self.data_store.is_dev_monitored(destination) and self.data_store.has_free_slot():
            self.data_store.add_dev_wallet(
                destination, label=f"transfert_{dev_address[:6]}_{destination[:6]}", scheme="sol_transfer", backtest_ratio=0.0,
                # AJOUTÉ suite à une demande explicite (19 août) : même
                # traitement que on_wallet_withdrawal — rattaché en
                # arrière-plan au parent, plus visible comme entrée séparée.
                # Toujours un Ruggeur ici (_creation_mode_wallets scope déjà
                # cette alerte aux wallets track_creation uniquement), pas
                # besoin d'exclusion Copy Trading comme pour les retraits.
                linked_to_parent=dev_address,
            )
            added_note = "\n\n_Adresse destinataire ajoutée au monitoring (en arrière-plan, rattachée à ce wallet)._"
        elif not self.data_store.has_free_slot():
            added_note = "\n\n⚠️ _Limite de wallets atteinte — adresse destinataire NON ajoutée._"

        reply_markup = None
        if signature:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔍 Voir sur Solscan", url=f"https://solscan.io/tx/{signature}")
            ]])

        await self.notifier.notify(
            "withdrawal_alert",
            f"💸 *Transfert SOL important détecté*\n\n"
            f"Dev : `{dev_address}` ({label})\n"
            f"Montant : `{amount_sol:.4f}` SOL (`{pct_of_balance:.0f}%` de son solde)\n"
            f"Destination : `{destination}`\n\n"
            f"_Ce dev encaisse peut-être et se prépare à disparaître._{added_note}",
            reply_markup=reply_markup,
        )

    async def on_copytrade_sell(self, wallet_address: str, token_mint: str, signature: str):
        """
        Callback du copytrade_listener : un wallet suivi vient de vendre.
        Deux comportements possibles selon le mode :
        - track_sell : clôture en miroir une position ouverte issue de ce wallet
        - buy_on_dev_sell : le dev vient de vendre SON PROPRE token qu'on
          surveillait → on achète maintenant (potentiellement meilleur point
          d'entrée que le bloc zéro, juste après le dump du dev)

        AJOUTÉ suite à une demande explicite : les wallets en mode
        track_creation (Ruggeur) sont maintenant AUSSI souscrits par
        copytrade_listener.py (pour la détection de transfert SOL), donc
        cette fonction reçoit désormais aussi leurs ventes de TOKEN. Rendu
        explicite ici — un dev qui vend son propre token ne doit JAMAIS
        déclencher une clôture de position ni aucune autre action de copy
        trading : sa position ouverte (achetée au bloc zéro) reste gérée
        UNIQUEMENT par son propre TP/SL, indépendamment de ce que fait le
        dev sur son wallet.
        """
        entry = self.data_store.state["monitored_dev_wallets"].get(wallet_address)
        if not entry:
            return

        # AJOUTÉ pour le nettoyage automatique.
        self.data_store.update_wallet_activity(wallet_address)

        mode = entry.get("mode")
        label = entry.get("label", wallet_address[:8] + "...")

        if mode == "track_creation":
            # Vente du dev sur son propre token — jamais copiée, jamais
            # utilisée pour clôturer quoi que ce soit. Le TP/SL de la
            # position gère ça tout seul, indépendamment.
            return

        if mode == "track_sell":
            for position in list(self.data_store.state.get("open_positions", [])):
                if position["source_wallet"] == wallet_address and position["token_mint"] == token_mint:
                    log.info(f"📋 Copy sell détecté : {label} a vendu {token_mint[:8]}... — clôture miroir.")
                    data = await backtest._get_pair_data(token_mint)
                    price = float(data.get("priceUsd", position["entry_price"]) or position["entry_price"])
                    change_pct = self.trader._pnl_pct(position, price)
                    await self.trader._close_remaining(position, price, change_pct, "COPY_SELL")
            return

        if mode == "buy_on_dev_sell":
            matched = self.data_store.check_pending_dev_sell_watch(
                wallet_address, token_mint, timeout_min=config.BUY_ON_DEV_SELL_TIMEOUT_MIN,
            )
            if not matched:
                return  # pas le bon token, ou surveillance expirée/déjà consommée

            log.info(f"💥 Buy on Dev Sell déclenché : {label} a vendu {token_mint[:8]}... — achat maintenant.")
            await self.trader.open_position(
                token_mint, wallet_address,
                reason=f"Buy on Dev Sell — achat juste après la vente du dev ({label})"
            )

    async def _evaluate_new_dev(self, dev_address: str, current_token_mint: str):
        """Trace les fonds, backteste l'historique, et décide d'ajouter au monitoring."""
        log.info(f"🔍 Évaluation du nouveau dev {dev_address[:8]}...")

        # AJOUTÉ (demande explicite, 19 août) : les 4 critères ci-dessous
        # étaient un mélange de constantes config.py figées et de valeurs
        # codées en dur ("3", "0.3") — maintenant lus depuis
        # state["auto_detection_settings"], modifiables depuis Telegram
        # (menu Settings → Détection auto). filters_enabled=False court-
        # circuite TOUT le reste de cette fonction : le dev est ajouté
        # directement, sans historique, sans ratio, sans régularité.
        # ⚠️ Interrupteur dangereux si activé en LIVE — ajoute littéralement
        # n'importe quel nouveau dev détecté sur toute la plateforme
        # Pump.fun, sans aucun filtre de qualité.
        det_settings = self.data_store.state.get("auto_detection_settings", dict(config.DEFAULT_AUTO_DETECTION_SETTINGS))

        if not det_settings.get("filters_enabled", True):
            # CORRIGÉ suite à un vrai bug trouvé (71 wallets pour une limite
            # de 30 observés en conditions réelles) : add_dev_wallet ne
            # vérifie JAMAIS lui-même la limite — c'est à l'appelant de le
            # faire avant. Ce chemin (filtres désactivés) l'oubliait
            # entièrement, contrairement au reste du bot (retraits,
            # transferts dev 90%) qui vérifie correctement.
            if not self.data_store.has_free_slot():
                log.info(f"   ⚠️ Limite de wallets atteinte — {dev_address[:8]}... NON ajouté (filtres désactivés).")
                return
            log.info(f"   ⚠️ Filtres de détection désactivés — ajout direct de {dev_address[:8]}... sans évaluation.")
            self.data_store.add_dev_wallet(
                dev_address, label="Auto-détecté (filtres désactivés)", scheme="inconnu", backtest_ratio=0.0,
            )
            await self.trader.open_position(
                current_token_mint, dev_address,
                reason="Nouveau dev — filtres de détection désactivés"
            )
            return

        min_tokens_created = det_settings.get("min_tokens_created", 3)
        min_ratio = det_settings.get("min_ratio", config.BACKTEST_MIN_RATIO)
        min_regularity = det_settings.get("min_regularity", 0.3)
        max_bundle_usd = det_settings.get("max_bundle_usd", config.MAX_FIRST_CANDLE_MARKET_CAP)

        # 1. Traçage des fonds (identifie le schéma)
        trace = await fund_tracer.classify_scheme(dev_address)
        scheme = trace.get("scheme", "inconnu")
        log.info(f"   Schéma détecté : {scheme} (funder: {(trace.get('funder_address') or 'inconnu')[:8]}...)")

        # 2. Historique des tokens créés
        past_tokens = await wallet_history.get_created_tokens(dev_address)
        if len(past_tokens) < min_tokens_created:
            log.info(f"   ⏭️  Historique insuffisant ({len(past_tokens)} tokens, min {min_tokens_created}) — pas assez de données pour juger.")
            return

        # 3. Backtest sur l'historique (inclut désormais le filtre "bundle > 15k"
        #    et la suggestion de TP basée sur la médiane des gains observés)
        result = await backtest.backtest_wallet(past_tokens, min_ratio=min_ratio, max_bundle_usd=max_bundle_usd)

        if not result["should_monitor"]:
            log.info(
                f"   ❌ Dev rejeté — ratio {result['ratio']:.2f} < {min_ratio} "
                f"(résultat cumulé {result['total_result_pct']:+.1f}%, "
                f"{result['skipped_bundle']} tokens exclus pour bundle trop haut)"
            )
            return

        # 3bis. Régularité de vente — reproduit "on veut un dev qui vend toujours
        # au même point" (voir analysis/wallet_history.py pour la méthode et ses limites)
        regularity = await wallet_history.check_sell_regularity(dev_address, past_tokens)
        if regularity["regularity_score"] < min_regularity:
            log.info(
                f"   ❌ Dev rejeté — trop irrégulier (score {regularity['regularity_score']:.2f} < {min_regularity}), "
                f"comportement pas assez prévisible pour un TP fixe fiable."
            )
            return

        # 3ter. Avis IA (Claude/Grok) — appui qualitatif, JAMAIS bloquant seul.
        # Un verdict "AVOID" de l'IA seul ne rejette pas le dev — il est juste
        # notifié pour que tu gardes un œil dessus. Skip silencieux si aucune
        # clé n'est configurée, OU si le toggle global IA ON/OFF est désactivé
        # (voir analysis/ai_advisor.py et 💰 Settings dans Telegram).
        if self.data_store.is_ai_enabled_globally():
            ai_verdict = await ai_advisor.get_ai_verdict(dev_address, result, regularity, scheme)
            if ai_verdict:
                log.info(f"   🤖 Avis IA ({ai_verdict['provider']}) : {ai_verdict['verdict']} — {ai_verdict['reasoning']}")

        # CORRIGÉ (même bug que le chemin "filtres désactivés" ci-dessus) :
        # vérification de la limite manquante ici aussi.
        if not self.data_store.has_free_slot():
            log.info(f"   ⚠️ Limite de wallets atteinte — {dev_address[:8]}... NON ajouté malgré un bon ratio ({result['ratio']:.2f}).")
            return

        # 4. Ajout au monitoring avec le TP suggéré par le backtest, + trade immédiat
        #    sur le token qui a déclenché la détection
        wallet_settings = {"tp_levels": [{"pct": result["suggested_tp_pct"], "sell_ratio": 1.0}]}
        self.data_store.add_dev_wallet(
            dev_address,
            label=f"Auto-détecté ({scheme})",
            scheme=scheme,
            backtest_ratio=result["ratio"],
            settings=wallet_settings,
        )
        log.info(
            f"   ✅ Dev ajouté au monitoring — ratio {result['ratio']:.2f}, "
            f"régularité {regularity['regularity_score']:.2f}, TP suggéré {result['suggested_tp_pct']:.0f}%"
        )

        if ai_verdict and ai_verdict["verdict"] != "GO":
            await self.trader.notifier.notify(
                "rugger_alert",
                f"🤖 *Avis IA* ({ai_verdict['provider']}) : {ai_verdict['verdict']}\n"
                f"Dev: `{dev_address[:8]}...`\nRaison: {ai_verdict['reasoning']}\n"
                f"_Ajouté quand même (backtest {result['ratio']:.2f}) — avis IA non bloquant._"
            ) if self.trader.notifier else None

        await self.trader.open_position(
            current_token_mint, dev_address,
            reason=f"Nouveau dev validé (ratio backtest {result['ratio']:.2f})"
        )


if __name__ == "__main__":
    bot = SniperBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        log.info("Arrêt du bot demandé par l'utilisateur.")
        sys.exit(0)
