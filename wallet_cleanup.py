"""
════════════════════════════════════════════════════════════════
NETTOYAGE AUTOMATIQUE DES WALLETS SURVEILLÉS
════════════════════════════════════════════════════════════════
AJOUTÉ suite à une demande explicite. Retire automatiquement du monitoring
les wallets qui :
  - N'ont eu AUCUNE activité depuis N jours (30 par défaut) — création de
    token pour un dev, achat/vente pour un trader
  - Ont enchaîné N pertes consécutives (7 par défaut) sur leurs N derniers
    trades, en s'arrêtant dès le premier gain ou la première position
    encore ouverte rencontrée en remontant depuis le plus récent

Les deux seuils, plus l'activation/désactivation globale, sont réglables
directement depuis Telegram (menu Settings → Nettoyage automatique) — voir
monitoring_list.get_cleanup_settings()/set_cleanup_settings(). Pas besoin
de modifier le code ni de redéployer pour ajuster.

Tourne en tâche de fond, comme protection_scanner.py — un passage toutes
les WALLET_CLEANUP_INTERVAL_S secondes (1h par défaut, pas besoin d'être
plus fréquent pour une tâche de maintenance).
"""

import asyncio
import logging
import time

import config
import wallet_history
import backtest

log = logging.getLogger("wallet_cleanup")


class WalletCleanup:
    def __init__(self, data_store, notifier=None):
        self.data_store = data_store
        self.notifier = notifier
        self._running = False

    async def start(self):
        self._running = True
        settings = self.data_store.get_cleanup_settings()
        log.info(
            f"🧹 Nettoyage automatique démarré — activé: {settings['enabled']}, "
            f"inactivité max: {settings['inactive_days']}j, "
            f"pertes consécutives max: {settings['max_consecutive_losses']}."
        )

        # CORRIGÉ suite à un vrai problème de coût RPC identifié : le premier
        # passage se lançait IMMÉDIATEMENT à chaque démarrage du bot, même si
        # un passage complet venait d'avoir lieu quelques minutes plus tôt
        # (ex: plusieurs redémarrages Railway rapprochés pendant une session
        # de debug) — répétant inutilement l'analyse la plus coûteuse du bot
        # (jusqu'à 150 transactions décodées par trade, jusqu'à 10 trades par
        # wallet, tous les wallets suivis). Persiste maintenant l'horodatage
        # du dernier passage RÉUSSI (state["wallet_cleanup_last_pass_at"],
        # survit à un redémarrage puisque sauvegardé dans le fichier de
        # données) et n'attend que le temps RESTANT avant le prochain passage
        # dû, au lieu de toujours repartir de zéro au démarrage.
        last_pass_at = self.data_store.state.get("wallet_cleanup_last_pass_at", 0)
        elapsed_since_last_pass = time.time() - last_pass_at
        wait_before_first_pass = max(0, config.WALLET_CLEANUP_INTERVAL_S - elapsed_since_last_pass)
        if wait_before_first_pass > 0:
            log.info(
                f"🧹 Dernier passage il y a {elapsed_since_last_pass / 60:.0f} min — "
                f"premier passage dans {wait_before_first_pass / 60:.0f} min (pas immédiat)."
            )
            await asyncio.sleep(wait_before_first_pass)

        while self._running:
            try:
                await self._run_cleanup_pass()
                self.data_store.state["wallet_cleanup_last_pass_at"] = time.time()
                self.data_store.save()
            except Exception as e:
                log.warning(f"⚠️ Erreur pendant le nettoyage automatique : {e}")
            await asyncio.sleep(config.WALLET_CLEANUP_INTERVAL_S)

    def stop(self):
        self._running = False

    async def _run_cleanup_pass(self):
        settings = self.data_store.get_cleanup_settings()
        if not settings.get("enabled", True):
            return

        wallets = dict(self.data_store.state.get("monitored_dev_wallets", {}))
        now = time.time()
        removed = []

        for address, entry in wallets.items():
            reason = None

            # ── 1. Inactivité ──
            last_activity = entry.get("last_activity_at", entry.get("added_at", now))
            inactive_days = (now - last_activity) / 86400
            if inactive_days >= settings["inactive_days"]:
                reason = f"inactif depuis {inactive_days:.0f} jour(s)"

            # ── 2. Pertes consécutives ── (seulement si pas déjà retiré pour inactivité)
            if not reason:
                try:
                    consecutive_losses = await self._count_consecutive_losses(
                        address, entry.get("mode", "track_creation")
                    )
                except Exception as e:
                    log.debug(f"Erreur calcul pertes consécutives pour {address[:8]}...: {e}")
                    consecutive_losses = 0
                if consecutive_losses >= settings["max_consecutive_losses"]:
                    reason = f"{consecutive_losses} pertes consécutives"

            if reason:
                self.data_store.remove_dev_wallet(address)
                removed.append((address, entry.get("label", address[:8] + "..."), reason))
                log.info(f"🧹 Wallet retiré du monitoring : {address[:8]}... ({reason})")

        if removed and self.notifier:
            lines = "\n".join(f"• `{label}` — {reason}" for _, label, reason in removed)
            await self.notifier.notify(
                "rugger_alert",
                f"🧹 *Nettoyage automatique* : {len(removed)} wallet(s) retiré(s) du monitoring\n\n{lines}",
            )

    async def _count_consecutive_losses(self, address: str, mode: str) -> int:
        """
        Compte les pertes consécutives les plus RÉCENTES — s'arrête dès le
        premier gain ou la première position encore ouverte ("➖") rencontrée
        en remontant depuis le trade le plus récent. get_created_tokens/
        get_recent_buys retournent déjà du plus récent au plus ancien.
        """
        if mode == "track_creation":
            trades = await wallet_history.get_created_tokens(address, max_results=10)
        elif mode in ("track_buy", "track_sell", "buy_on_dev_sell"):
            trades = await wallet_history.get_recent_buys(address, max_results=10)
        else:
            return 0

        if not trades:
            return 0

        consecutive = 0
        for t in trades:
            detail = await backtest.get_detailed_trade_info(
                t["token_mint"], purchase_block_time=t.get("block_time"),
                sol_spent=t.get("sol_spent"), tokens_received=t.get("tokens_received"),
                max_transactions=config.WALLET_CLEANUP_MAX_TRANSACTIONS_SCANNED,
            )
            if detail["result_pct"] < 0:
                consecutive += 1
            else:
                break  # gain ou neutre -> la série de pertes s'arrête ici

        return consecutive
