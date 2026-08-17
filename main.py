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

        if config.TRADING_MODE == "LIVE":
            self.trader = LiveTrader(self.data_store, notifier=self.notifier)
        else:
            self.trader = PaperTrader(self.data_store, notifier=self.notifier)

        self.listener = NewTokenListener(on_new_token=self.on_new_token)
        self.copytrade_listener = CopyTradeListener(
            self.data_store, on_buy=self.on_copytrade_buy, on_sell=self.on_copytrade_sell,
            on_large_sol_transfer=self.on_dev_large_sol_transfer,
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
        tasks = [self.protection_scanner.start(), self.copytrade_listener.start(), self.rug_scanner.start(), self.wallet_cleanup.start()]
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

        # Cas 2 : nouveau dev → pipeline complet d'évaluation
        await self._evaluate_new_dev(dev_address, token_mint)

    async def on_copytrade_buy(self, wallet_address: str, token_mint: str, signature: str):
        """
        Callback du copytrade_listener : un wallet suivi en mode track_buy vient
        d'acheter un token. Applique Max Token Age et Follow Cooldown (les deux
        étaient configurables depuis longtemps mais jamais vérifiés avant cette
        version), puis ouvre une position miroir.
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

        log.info(f"📋 Copy trade détecté : {label} a acheté {token_mint[:8]}... (tx {signature[:12]}...)")
        await self.trader.open_position(
            token_mint, wallet_address,
            reason=f"Copy trade — achat détecté chez {label}"
        )

    async def on_dev_large_sol_transfer(self, dev_address: str, destination: str, amount_sol: float, pct_of_balance: float):
        """
        Callback du copytrade_listener : un dev surveillé (mode
        track_creation) vient de transférer une grosse partie de son solde
        SOL vers une autre adresse — signal fort qu'il encaisse et se
        prépare à disparaître. AJOUTÉ suite à une demande explicite.

        Deux actions : (1) alerte Telegram immédiate, (2) ajout automatique
        de l'adresse DESTINATAIRE au monitoring — elle pourrait être un
        autre wallet contrôlé par la même personne, ou une piste utile pour
        la méthode "adresse intermédiaire".
        """
        entry = self.data_store.state["monitored_dev_wallets"].get(dev_address, {})
        label = entry.get("label", dev_address[:8] + "...")

        log.warning(
            f"💸 ALERTE : {label} a transféré {amount_sol:.4f} SOL ({pct_of_balance:.0f}% de son solde) "
            f"vers {destination[:8]}..."
        )
        await self.notifier.notify(
            "rugger_alert",
            f"💸 *Transfert SOL important détecté*\n\n"
            f"Dev : `{dev_address}` ({label})\n"
            f"Montant : `{amount_sol:.4f}` SOL (`{pct_of_balance:.0f}%` de son solde)\n"
            f"Destination : `{destination}`\n\n"
            f"_Ce dev encaisse peut-être et se prépare à disparaître. "
            f"L'adresse destinataire a été ajoutée au monitoring._",
        )

        if not self.data_store.is_dev_monitored(destination) and self.data_store.has_free_slot():
            self.data_store.add_dev_wallet(
                destination, label=f"transfert_{dev_address[:8]}", scheme="sol_transfer", backtest_ratio=0.0,
            )
            log.info(f"➕ Adresse destinataire ajoutée au monitoring suite au transfert : {destination[:8]}...")

    async def on_copytrade_sell(self, wallet_address: str, token_mint: str, signature: str):
        """
        Callback du copytrade_listener : un wallet suivi vient de vendre.
        Deux comportements possibles selon le mode :
        - track_sell : clôture en miroir une position ouverte issue de ce wallet
        - buy_on_dev_sell : le dev vient de vendre SON PROPRE token qu'on
          surveillait → on achète maintenant (potentiellement meilleur point
          d'entrée que le bloc zéro, juste après le dump du dev)
        """
        entry = self.data_store.state["monitored_dev_wallets"].get(wallet_address)
        if not entry:
            return

        # AJOUTÉ pour le nettoyage automatique.
        self.data_store.update_wallet_activity(wallet_address)

        mode = entry.get("mode")
        label = entry.get("label", wallet_address[:8] + "...")

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

        # 1. Traçage des fonds (identifie le schéma)
        trace = await fund_tracer.classify_scheme(dev_address)
        scheme = trace.get("scheme", "inconnu")
        log.info(f"   Schéma détecté : {scheme} (funder: {(trace.get('funder_address') or 'inconnu')[:8]}...)")

        # 2. Historique des tokens créés
        past_tokens = await wallet_history.get_created_tokens(dev_address)
        if len(past_tokens) < 3:
            log.info(f"   ⏭️  Historique insuffisant ({len(past_tokens)} tokens) — pas assez de données pour juger.")
            return

        # 3. Backtest sur l'historique (inclut désormais le filtre "bundle > 15k"
        #    et la suggestion de TP basée sur la médiane des gains observés)
        result = await backtest.backtest_wallet(past_tokens)

        if not result["should_monitor"]:
            log.info(
                f"   ❌ Dev rejeté — ratio {result['ratio']:.2f} < {config.BACKTEST_MIN_RATIO} "
                f"(résultat cumulé {result['total_result_pct']:+.1f}%, "
                f"{result['skipped_bundle']} tokens exclus pour bundle trop haut)"
            )
            return

        # 3bis. Régularité de vente — reproduit "on veut un dev qui vend toujours
        # au même point" (voir analysis/wallet_history.py pour la méthode et ses limites)
        regularity = await wallet_history.check_sell_regularity(dev_address, past_tokens)
        if regularity["regularity_score"] < 0.3:
            log.info(
                f"   ❌ Dev rejeté — trop irrégulier (score {regularity['regularity_score']:.2f}), "
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
