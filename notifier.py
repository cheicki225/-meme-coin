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

ALWAYS_ON = {"buy_failed", "sell_failed", "bot_errors"}


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
        """
        if category not in ALWAYS_ON:
            prefs = self.data_store.get_notification_prefs()
            if not prefs.get(category, True):
                return  # notification désactivée par l'utilisateur

        chat_id = self.data_store.state.get("telegram_chat_id")
        if not self.bot or not chat_id:
            log.info(f"[notify:{category}] (Telegram non connecté) {text}")
            return

        try:
            await self.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=reply_markup)
        except Exception as e:
            # CORRIGÉ suite à un vrai risque identifié : le nom d'un token
            # (maintenant inséré dynamiquement dans "Buy Confirmed") peut
            # contenir des caractères spéciaux Markdown (underscore,
            # astérisque) et casser le parsing Telegram — repli en texte
            # brut plutôt que de perdre la notification entièrement, comme
            # déjà fait ailleurs dans le bot pour ce même risque.
            log.warning(f"Erreur Markdown notification Telegram ({category}): {e} — repli en texte brut.")
            try:
                plain_text = text.replace("*", "").replace("`", "").replace("_", "")
                await self.bot.send_message(chat_id=chat_id, text=plain_text, reply_markup=reply_markup)
            except Exception as e2:
                log.error(f"Échec du repli texte brut pour notification ({category}): {e2}")

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
            log.error(f"Erreur envoi photo Telegram ({category}): {e}")
