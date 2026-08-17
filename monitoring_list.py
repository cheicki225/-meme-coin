"""
════════════════════════════════════════════════════════════════
MONITORING LIST — Wallets/adresses sous surveillance + persistance
════════════════════════════════════════════════════════════════
Équivalent du "F Project monitoring" de la vidéo : la liste des
adresses (dev wallets) que le bot surveille en mode sniping de dev,
avec leurs paramètres spécifiques (TP multi-niveaux, market cap
min/max, snipe delay, no-activity-sell, etc — voir config.py pour
le détail de chaque réglage).

Ajoute également :
- Les "protection targets" (adresses mère / exchange à surveiller
  en continu pour repérer PROACTIVEMENT les nouveaux wallets financés,
  avant même qu'ils créent un token — c'est ça qui permet le vrai
  "bloc zéro" du sniping de dev).
- Les presets réutilisables (sauvegarder une config de settings sous
  un nom, l'appliquer rapidement à un nouveau wallet).
"""

import copy
import glob
import json
import logging
import os
import shutil
import time

import config

BACKUP_DIR_NAME = "backups"
BACKUP_INTERVAL_S = 300   # 5 minutes entre deux backups automatiques
MAX_BACKUPS = 30          # rotation — garde les 30 dernières, supprime les plus vieilles

log = logging.getLogger("monitoring_list")


class DataStore:
    """Charge/sauvegarde l'état complet du bot dans un fichier JSON."""

    def __init__(self, path: str = None):
        self.path = path or config.DATA_FILE
        self.state = self._load()
        self._last_backup_time = 0
        self._backup_dir = os.path.join(os.path.dirname(os.path.abspath(self.path)) or ".", BACKUP_DIR_NAME)
        # Backup immédiat au démarrage — capture le dernier état connu avant
        # que cette session ne commence à modifier quoi que ce soit.
        if os.path.exists(self.path):
            self._create_backup(reason="startup")

    def _load(self) -> dict:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    default = self._default_state()
                    for key, value in default.items():
                        loaded.setdefault(key, value)
                    self._backfill_wallet_settings(loaded)
                    return loaded
            except (json.JSONDecodeError, OSError) as e:
                log.error(f"Erreur lecture {self.path}: {e} — réinitialisation.")

        return self._default_state()

    def _backfill_wallet_settings(self, state: dict):
        """
        CORRIGÉ suite à un vrai bug trouvé : add_dev_wallet() copie
        DEFAULT_WALLET_SETTINGS au moment où le wallet est ajouté — un
        wallet ajouté avant qu'on introduise un nouveau réglage (Jito, mode
        vitesse, stratégies avancées...) reste figé sans cette clé. Certains
        endroits du code y accèdent directement (s['clé']) sans .get(),
        provoquant un plantage silencieux. Comble ici les clés manquantes
        sur TOUS les wallets existants, sans jamais écraser une valeur déjà
        personnalisée — juste ajouter ce qui manque.
        """
        wallets = state.get("monitored_dev_wallets", {})
        backfilled_count = 0
        for address, entry in wallets.items():
            settings = entry.setdefault("settings", {})
            for key, default_value in config.DEFAULT_WALLET_SETTINGS.items():
                if key not in settings:
                    settings[key] = copy.deepcopy(default_value)
                    backfilled_count += 1
        if backfilled_count > 0:
            log.info(f"🔧 {backfilled_count} réglage(s) manquant(s) comblé(s) sur des wallets existants (rattrapage).")

    def _default_state(self) -> dict:
        return {
            "monitored_dev_wallets": {},        # {address: {label, scheme, mode, backtest_ratio, settings}}
            "protection_targets": {},           # {label: {type, address, amount_sol, tolerance, preset}}
            "presets": {},                      # {name: settings_dict}
            "open_positions": [],
            "closed_positions": [],
            "total_pnl_usd": 0.0,
            "session_losses_usd": 0.0,
            "trades": [],
            "global_auto_buy": config.GLOBAL_AUTO_BUY_DEFAULT,
            "global_auto_sell": config.GLOBAL_AUTO_SELL_DEFAULT,
            "global_ai_enabled": True,
            "notification_prefs": dict(config.DEFAULT_NOTIFICATION_PREFS),
            "telegram_chat_id": None,
            "referral": dict(config.DEFAULT_REFERRAL_STATE),
            "linked_wallet_groups": {},
            "pending_dev_sell_watch": {},
        }

    def save(self):
        # CORRIGÉ suite à une question explicite sur la persistance des
        # données : cette fonction n'a jamais créé le dossier parent de
        # self.path avant d'écrire. Si DATA_FILE pointe vers un chemin de
        # volume (ex: /app/data/sniper_data.json) et que ce dossier n'existe
        # pas encore, l'écriture échouait SILENCIEUSEMENT (juste une ligne
        # de log, aucun plantage visible) — les données n'étaient alors
        # JAMAIS réellement sauvegardées, sans que rien ne le signale
        # clairement dans l'usage normal du bot.
        try:
            parent_dir = os.path.dirname(self.path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False)
        except OSError as e:
            log.error(f"Erreur sauvegarde {self.path}: {e}")
            return

        # Backup périodique (pas à chaque save() — save() est appelé très
        # fréquemment, un backup à chaque fois serait excessif en I/O disque)
        now = time.time()
        if now - self._last_backup_time >= BACKUP_INTERVAL_S:
            self._create_backup(reason="periodic")
            self._last_backup_time = now

    def _create_backup(self, reason: str = "periodic"):
        """Copie horodatée de sniper_data.json dans backups/, avec rotation
        (garde les MAX_BACKUPS plus récentes, supprime les plus vieilles)."""
        if not os.path.exists(self.path):
            return
        try:
            os.makedirs(self._backup_dir, exist_ok=True)
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            backup_filename = f"sniper_data_{timestamp}_{reason}.json"
            backup_path = os.path.join(self._backup_dir, backup_filename)
            shutil.copy2(self.path, backup_path)
            self._prune_old_backups()
            log.debug(f"💾 Backup créé : {backup_filename}")
        except Exception as e:
            log.error(f"Erreur création backup : {e}")

    def _prune_old_backups(self):
        try:
            pattern = os.path.join(self._backup_dir, "sniper_data_*.json")
            backups = sorted(glob.glob(pattern))  # tri alphabétique = tri chronologique (format horodatage)
            while len(backups) > MAX_BACKUPS:
                oldest = backups.pop(0)
                os.remove(oldest)
        except Exception as e:
            log.error(f"Erreur rotation des backups : {e}")

    def list_backups(self) -> list:
        """Retourne la liste des backups disponibles, du plus récent au plus ancien."""
        pattern = os.path.join(self._backup_dir, "sniper_data_*.json")
        return sorted(glob.glob(pattern), reverse=True)

    def restore_from_backup(self, backup_path: str) -> bool:
        """
        Restaure l'état depuis un fichier de backup. Crée d'abord un backup
        de l'état ACTUEL (reason="pre_restore") avant d'écraser — au cas où
        la restauration elle-même serait une erreur, rien n'est perdu.
        """
        if not os.path.exists(backup_path):
            log.error(f"Backup introuvable : {backup_path}")
            return False
        try:
            self._create_backup(reason="pre_restore")
            with open(backup_path, "r", encoding="utf-8") as f:
                restored_state = json.load(f)
            self.state = restored_state
            self.save()
            log.info(f"✅ État restauré depuis {os.path.basename(backup_path)}")
            return True
        except Exception as e:
            log.error(f"Erreur restauration depuis {backup_path}: {e}")
            return False

    # ── Gestion des wallets dev surveillés ──────────────────────
    def add_dev_wallet(self, address: str, label: str, scheme: str, backtest_ratio: float,
                        mode: str = "track_creation", settings: dict = None):
        """
        mode: "track_creation" (sniping de dev — défaut) ou "track_buy" (copy trade, hors scope ici)
        settings: dict partiel qui override config.DEFAULT_WALLET_SETTINGS
        """
        final_settings = copy.deepcopy(config.DEFAULT_WALLET_SETTINGS)
        if settings:
            final_settings.update(settings)

        # AJOUTÉ pour le nettoyage automatique (wallet_cleanup.py) : horodatage
        # de l'ajout ET de la dernière activité connue — nécessaire pour
        # détecter l'inactivité. Initialisé à "maintenant" pour ne pas
        # supprimer un wallet fraîchement ajouté avant même de lui laisser
        # une chance.
        now = time.time()
        self.state["monitored_dev_wallets"][address] = {
            "label": label,
            "scheme": scheme,
            "mode": mode,
            "backtest_ratio": backtest_ratio,
            "settings": final_settings,
            "already_bought": False,  # pour buy_only_once
            "added_at": now,
            "last_activity_at": now,
        }
        self.save()
        log.info(f"➕ Wallet dev ajouté au monitoring : {address[:8]}... ({label}, mode={mode})")

    def update_wallet_activity(self, address: str):
        """AJOUTÉ pour le nettoyage automatique — appelé à chaque activité
        détectée (création de token, achat, vente) pour repousser le délai
        d'inactivité. Ne fait rien si le wallet n'est pas/plus monitoré."""
        entry = self.state["monitored_dev_wallets"].get(address)
        if entry:
            entry["last_activity_at"] = time.time()
            self.save()

    def get_cleanup_settings(self) -> dict:
        """AJOUTÉ pour le nettoyage automatique — réglages modifiables
        depuis Telegram, avec repli sur les valeurs par défaut de config.py
        si jamais explicitement changés."""
        defaults = {
            "enabled": config.WALLET_CLEANUP_ENABLED,
            "inactive_days": config.WALLET_CLEANUP_INACTIVE_DAYS,
            "max_consecutive_losses": config.WALLET_CLEANUP_MAX_CONSECUTIVE_LOSSES,
        }
        stored = self.state.get("wallet_cleanup_settings", {})
        return {**defaults, **stored}

    def set_cleanup_settings(self, **kwargs):
        """AJOUTÉ pour le nettoyage automatique — met à jour un ou plusieurs
        réglages (enabled, inactive_days, max_consecutive_losses)."""
        settings = self.state.setdefault("wallet_cleanup_settings", {})
        settings.update({k: v for k, v in kwargs.items() if v is not None})
        self.save()

    def remove_dev_wallet(self, address: str):
        self.state["monitored_dev_wallets"].pop(address, None)
        self.save()

    def is_dev_monitored(self, address: str) -> bool:
        return address in self.state["monitored_dev_wallets"]

    def get_short_id(self, address: str) -> str:
        """
        CORRIGÉ suite à un vrai bug trouvé : les callback_data Telegram sont
        plafonnés à 64 caractères — combiné à une adresse Solana longue
        (jusqu'à 44 caractères) et un préfixe/suffixe descriptif, plus de 50
        callback_data différents dépassaient cette limite, provoquant des
        erreurs 'Button_data_invalid' sur presque toutes les pages de config.

        Retourne un identifiant court et stable ("sid" + 8 caractères hex)
        pour une adresse, à utiliser dans les callback_data à la place de
        l'adresse complète. Maintient une table de correspondance pour
        pouvoir la retrouver ensuite (voir resolve_short_id).
        """
        import hashlib
        short = "sid" + hashlib.sha256(address.encode()).hexdigest()[:8]
        self.state.setdefault("short_id_map", {})[short] = address
        return short

    def resolve_short_id(self, short_id: str) -> str:
        """Retrouve l'adresse complète à partir d'un identifiant court. Si
        l'entrée n'existe pas (jamais générée, ou état corrompu), retourne
        l'identifiant tel quel plutôt que de planter — le code appelant
        traitera alors simplement "wallet introuvable" comme avant."""
        return self.state.get("short_id_map", {}).get(short_id, short_id)

    def get_wallet_settings(self, address: str) -> dict:
        entry = self.state["monitored_dev_wallets"].get(address, {})
        return entry.get("settings", copy.deepcopy(config.DEFAULT_WALLET_SETTINGS))

    def mark_bought(self, address: str):
        if address in self.state["monitored_dev_wallets"]:
            self.state["monitored_dev_wallets"][address]["already_bought"] = True
            self.save()

    def has_already_bought(self, address: str) -> bool:
        entry = self.state["monitored_dev_wallets"].get(address, {})
        return entry.get("already_bought", False)

    def update_wallet_settings(self, address: str, partial_settings: dict):
        if address not in self.state["monitored_dev_wallets"]:
            return
        self.state["monitored_dev_wallets"][address]["settings"].update(partial_settings)
        self.save()

    def rename_wallet(self, address: str, new_label: str):
        if address in self.state["monitored_dev_wallets"]:
            self.state["monitored_dev_wallets"][address]["label"] = new_label
            self.save()

    def list_wallets(self) -> dict:
        return self.state["monitored_dev_wallets"]

    def count_wallets(self) -> int:
        return len(self.state["monitored_dev_wallets"])

    def count_wallets_by_mode(self) -> tuple:
        """
        AJOUTÉ suite à une demande explicite : l'en-tête du menu principal
        affichait un seul compteur "Ruggers: X/30" qui mélangeait en fait
        TOUS les wallets monitorés, y compris ceux en mode track_buy/
        track_sell (copy trading) — donnant l'impression trompeuse qu'un
        wallet ajouté en Copy Trading avait disparu ou n'avait pas été
        ajouté, alors qu'il était juste compté sous la mauvaise étiquette.

        Retourne (nb_ruggers, nb_copytrading) — même logique de filtrage par
        mode que show_ruggers_menu()/show_copytrade_menu() dans telegram_bot.py.
        """
        ruggers = sum(1 for e in self.state["monitored_dev_wallets"].values()
                      if e.get("mode", "track_creation") in ("track_creation", "buy_on_dev_sell"))
        copytrading = sum(1 for e in self.state["monitored_dev_wallets"].values()
                           if e.get("mode") in ("track_buy", "track_sell"))
        return ruggers, copytrading

    def has_free_slot(self) -> bool:
        return self.count_wallets() < config.MAX_MONITORED_WALLETS

    # ── Auto-buy / auto-sell (par wallet + globaux) ─────────────
    def is_auto_buy_active(self, address: str) -> bool:
        if not self.state.get("global_auto_buy", True):
            return False
        entry = self.state["monitored_dev_wallets"].get(address, {})
        settings = entry.get("settings", {})
        return settings.get("auto_buy", True) and not settings.get("paused", False)

    def is_auto_sell_active(self, address: str) -> bool:
        if not self.state.get("global_auto_sell", True):
            return False
        entry = self.state["monitored_dev_wallets"].get(address, {})
        return entry.get("settings", {}).get("auto_sell", True)

    def toggle_global_auto_buy(self) -> bool:
        self.state["global_auto_buy"] = not self.state.get("global_auto_buy", True)
        self.save()
        return self.state["global_auto_buy"]

    def toggle_global_auto_sell(self) -> bool:
        self.state["global_auto_sell"] = not self.state.get("global_auto_sell", True)
        self.save()
        return self.state["global_auto_sell"]

    def toggle_global_ai(self) -> bool:
        self.state["global_ai_enabled"] = not self.state.get("global_ai_enabled", True)
        self.save()
        return self.state["global_ai_enabled"]

    def is_ai_enabled_globally(self) -> bool:
        return self.state.get("global_ai_enabled", True)

    # ── Max Loss Counter ─────────────────────────────────────────
    def record_trade_result(self, address: str, is_win: bool) -> bool:
        """
        Met à jour le compteur de pertes consécutives pour ce wallet.
        Retourne True si le seuil vient d'être atteint (auto-buy mis en pause).
        """
        entry = self.state["monitored_dev_wallets"].get(address)
        if not entry:
            return False

        settings = entry["settings"]
        if is_win:
            settings["consecutive_losses"] = 0
            settings["paused"] = False
            self.save()
            return False

        settings["consecutive_losses"] = settings.get("consecutive_losses", 0) + 1
        max_losses = settings.get("max_consecutive_losses", 0)

        just_paused = False
        if max_losses and settings["consecutive_losses"] >= max_losses and not settings.get("paused", False):
            settings["paused"] = True
            just_paused = True

        self.save()
        return just_paused

    def unpause_wallet(self, address: str):
        entry = self.state["monitored_dev_wallets"].get(address)
        if entry:
            entry["settings"]["paused"] = False
            entry["settings"]["consecutive_losses"] = 0
            self.save()

    # ── Follow Cooldown (copytrade) ──────────────────────────────
    def check_and_update_follow_cooldown(self, address: str) -> bool:
        """
        Vérifie le Follow Cooldown pour un wallet copytrade : retourne False
        si un achat copié a eu lieu trop récemment (à ignorer), True si
        l'achat peut être copié (et met à jour last_copy_trade_time dans ce cas).
        """
        import time
        entry = self.state["monitored_dev_wallets"].get(address)
        if not entry:
            return True

        settings = entry["settings"]
        cooldown_s = settings.get("follow_cooldown_s", 0)
        if not cooldown_s:
            return True

        last_time = settings.get("last_copy_trade_time")
        now = time.time()
        if last_time and (now - last_time) < cooldown_s:
            return False

        settings["last_copy_trade_time"] = now
        self.save()
        return True

    # ── Buy on Dev Sell — surveillance temporaire post-création ──
    def set_pending_dev_sell_watch(self, dev_address: str, token_mint: str):
        """Enregistre qu'on attend la vente du dev sur CE token précis pour racheter après."""
        import time
        watches = self.state.setdefault("pending_dev_sell_watch", {})
        watches[dev_address] = {"token_mint": token_mint, "created_at": time.time()}
        self.save()

    def check_pending_dev_sell_watch(self, dev_address: str, sold_token_mint: str, timeout_min: float = 30) -> bool:
        """
        Vérifie si la vente détectée correspond bien au token qu'on surveille
        pour ce dev, et que ça n'a pas expiré. Si oui, retourne True et efface
        la surveillance (one-shot — on ne rachète qu'une fois par création).
        """
        import time
        watches = self.state.get("pending_dev_sell_watch", {})
        watch = watches.get(dev_address)
        if not watch:
            return False

        if watch["token_mint"] != sold_token_mint:
            return False

        age_min = (time.time() - watch["created_at"]) / 60
        if age_min > timeout_min:
            watches.pop(dev_address, None)
            self.save()
            return False

        watches.pop(dev_address, None)
        self.save()
        return True

    # ── Notifications ────────────────────────────────────────────
    def get_notification_prefs(self) -> dict:
        return self.state.get("notification_prefs", dict(config.DEFAULT_NOTIFICATION_PREFS))

    def toggle_notification_pref(self, key: str) -> bool:
        prefs = self.state.setdefault("notification_prefs", dict(config.DEFAULT_NOTIFICATION_PREFS))
        prefs[key] = not prefs.get(key, True)
        self.save()
        return prefs[key]

    def set_telegram_chat_id(self, chat_id: int):
        self.state["telegram_chat_id"] = chat_id
        self.save()

    # ── Referral (simulé) ────────────────────────────────────────
    def get_referral_state(self) -> dict:
        return self.state.setdefault("referral", dict(config.DEFAULT_REFERRAL_STATE))

    def generate_referral_code(self, chat_id: int) -> str:
        import hashlib
        code = hashlib.sha256(str(chat_id).encode()).hexdigest()[:8].upper()
        referral = self.get_referral_state()
        referral["code"] = code
        self.save()
        return code

    # ── Presets (réglages réutilisables, comme "save preset" dans F Project) ─
    def save_preset(self, name: str, settings: dict):
        self.state["presets"][name] = settings
        self.save()
        log.info(f"💾 Preset sauvegardé : {name}")

    def apply_preset(self, address: str, preset_name: str):
        preset = self.state["presets"].get(preset_name)
        if not preset:
            log.warning(f"Preset introuvable : {preset_name}")
            return
        self.update_wallet_settings(address, preset)

    def delete_preset(self, name: str):
        self.state["presets"].pop(name, None)
        self.save()

    # ── Protection targets (surveillance proactive schéma mère/exchange) ─
    def add_protection_target(self, label: str, target_type: str, address: str,
                               amount_sol: float = None, tolerance: float = None,
                               preset_name: str = None, parent_address: str = None):
        """
        target_type: "exchange" (montant fixe) ou "mere" (tous les transferts sortants)
        parent_address: si défini, les nouvelles adresses enfants héritent selon le
                        toggle inherit_settings du parent (voir _resolve_child_settings
                        dans protection_scanner.py)
        """
        self.state["protection_targets"][label] = {
            "type": target_type,
            "address": address,
            "amount_sol": amount_sol,
            "tolerance": tolerance or config.FIXED_AMOUNT_TOLERANCE_SOL,
            "preset_name": preset_name,
            "parent_address": parent_address,
            "known_recipients": [],  # évite de re-traiter les mêmes adresses à chaque scan
        }
        self.save()
        log.info(f"➕ Protection ajoutée : {label} ({target_type}, {address[:8]}...)")

    def remove_protection_target(self, label: str):
        self.state["protection_targets"].pop(label, None)
        self.save()

    def mark_recipient_known(self, label: str, recipient: str):
        target = self.state["protection_targets"].get(label)
        if target and recipient not in target["known_recipients"]:
            target["known_recipients"].append(recipient)
            self.save()

    def is_recipient_known(self, label: str, recipient: str) -> bool:
        target = self.state["protection_targets"].get(label, {})
        return recipient in target.get("known_recipients", [])

    # ── Groupes de wallets liés (pour Front Run Sell — voir paper_trader.py) ─
    def add_linked_wallet(self, group_label: str, address: str):
        groups = self.state.setdefault("linked_wallet_groups", {})
        group = groups.setdefault(group_label, [])
        if address not in group:
            group.append(address)
            self.save()

    def get_linked_group_for_wallet(self, address: str) -> list:
        """Retourne le groupe de wallets liés contenant cette adresse (pour Front Run Sell)."""
        groups = self.state.get("linked_wallet_groups", {})
        for group_label, members in groups.items():
            if address in members:
                return members
        return [address]
