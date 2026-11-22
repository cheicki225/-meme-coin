"""
════════════════════════════════════════════════════════════════
NOTIFIER — Envoi de notifications Telegram
════════════════════════════════════════════════════════════════
Couche fine utilisée par paper_trader, protection_scanner et le bot
lui-même pour envoyer des messages. Respecte les préférences de
notification de l'utilisateur (menu Settings → Notifications).

Buy Failed / Sell Failed / Bot Errors sont toujours envoyés (non
désactivables), comme dans la doc F Project.
"""

import logging

log = logging.getLogger("notifier")

ALWAYS_ON = {"buy_failed", "sell_failed", "bot_errors", "risk_alert", "rpc_alert", "collector_alert"}


class Notifier:
    def __init__(self, data_store, bot=None):
        """
        data_store: instance de execution.monitoring_list.DataStore
        bot: instance telegram.Bot — peut être None si le bot Telegram n'est pas
             encore démarré ou n'est pas configuré (le notifier log alors seulement).
        """
        self.data_store = data_store
        self.bot = bot

    def set_bot(self, bot):
        self.bot = bot

    async def notify(self, category: str, text: str, reply_markup=None):
        """
        category: une des clés de config.DEFAULT_NOTIFICATION_PREFS, ou une clé
                  de ALWAYS_ON pour les alertes critiques non désactivables.

        Returns True when Telegram accepts the message, or False when it was
        disabled, unavailable, or failed so monitoring callers can retry.
        """
        if category not in ALWAYS_ON:
            prefs = self.data_store.get_notification_prefs()
            if not prefs.get(category, True):
                return False  # notification désactivée par l'utilisateur

        chat_id = self.data_store.state.get("telegram_chat_id")
        if not self.bot or not chat_id:
            # Do not copy notification bodies into logs: they may contain
            # private operational details and Telegram will not deliver them.
            log.info("[notify:%s] Telegram non connecté; message non envoyé", category)
            return False

        try:
            await self.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=reply_markup)
            return True
        except Exception as e:
            # CORRIGÉ suite à un vrai risque identifié : le nom d'un token
            # (maintenant inséré dynamiquement dans "Buy Confirmed") peut
            # contenir des caractères spéciaux Markdown (underscore,
            # astérisque) et casser le parsing Telegram — repli en texte
            # brut plutôt que de perdre la notification entièrement, comme
            # déjà fait ailleurs dans le bot pour ce même risque.
            # Telegram exception text can include the bot token embedded in its API URL.
            log.warning("Telegram Markdown notification failed for %s (%s); retrying as plain text", category, type(e).__name__)
            try:
                await self.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
                return True
            except Exception as e2:
                log.error("Telegram plain-text notification failed for %s (%s)", category, type(e2).__name__)
                return False

    async def notify_photo(self, category: str, photo_bytes: bytes, caption: str = "", reply_markup=None):
        """Envoie une image (ex: carte PNL générée par pnl_card.py) avec légende optionnelle."""
        if category not in ALWAYS_ON:
            prefs = self.data_store.get_notification_prefs()
            if not prefs.get(category, True):
                return

        chat_id = self.data_store.state.get("telegram_chat_id")
        if not self.bot or not chat_id:
            log.info(f"[notify_photo:{category}] (Telegram non connecté) {caption}")
            return

        try:
            import io
            await self.bot.send_photo(
                chat_id=chat_id, photo=io.BytesIO(photo_bytes),
                caption=caption, parse_mode="Markdown", reply_markup=reply_markup,
            )
        except Exception as e:
            log.error("Telegram photo notification failed for %s (%s)", category, type(e).__name__)
