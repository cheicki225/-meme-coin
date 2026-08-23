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
                    self._migrate_force_buy_only_once(loaded)
                    self._migrate_force_profit_trail_enabled(loaded)
                    self._migrate_reset_legacy_already_bought(loaded)
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

    def _migrate_force_buy_only_once(self, state: dict):
        """
        MIGRATION PONCTUELLE (demande explicite, 19 août 2026) : plusieurs
        wallets existants avaient "buy_only_once" à False alors que le
        défaut global (config.DEFAULT_WALLET_SETTINGS) est True — valeur
        figée au moment de leur ajout, jamais mise à jour depuis. Le
        backfill ci-dessus ne comble que les clés MANQUANTES, jamais une
        valeur déjà présente (même si elle vient d'un ancien défaut), donc
        il ne suffisait pas à corriger ce cas. Force ici "buy_only_once" à
        True sur TOUS les wallets existants, UNE SEULE FOIS (flag
        state["_migration_buy_only_once_forced_v1"]) — après ce passage,
        le bouton Telegram redevient seul maître du réglage par wallet,
        cette migration ne repassera plus jamais dessus.
        """
        if state.get("_migration_buy_only_once_forced_v1"):
            return
        wallets = state.get("monitored_dev_wallets", {})
        forced_count = 0
        for address, entry in wallets.items():
            settings = entry.setdefault("settings", {})
            if not settings.get("buy_only_once"):
                settings["buy_only_once"] = True
                forced_count += 1
        state["_migration_buy_only_once_forced_v1"] = True
        if forced_count > 0:
            log.info(f"🔧 Migration ponctuelle : buy_only_once forcé à True sur {forced_count} wallet(s) existant(s).")

    def _migrate_force_profit_trail_enabled(self, state: dict):
        """
        MIGRATION PONCTUELLE (demande explicite, 19 août 2026) : même
        principe que _migrate_force_buy_only_once ci-dessus, pour
        "profit_trail_enabled" — passé à True par défaut (au lieu de
        False), mais les wallets existants avaient déjà cette clé à False,
        donc le backfill (qui ne comble que les clés MANQUANTES) ne
        suffisait pas. Force ici à True sur tous les wallets existants,
        UNE SEULE FOIS (flag state["_migration_profit_trail_forced_v1"]) —
        le bouton Telegram redevient ensuite seul maître du réglage par
        wallet.
        """
        if state.get("_migration_profit_trail_forced_v1"):
            return
        wallets = state.get("monitored_dev_wallets", {})
        forced_count = 0
        for address, entry in wallets.items():
            settings = entry.setdefault("settings", {})
            if not settings.get("profit_trail_enabled"):
                settings["profit_trail_enabled"] = True
                forced_count += 1
        state["_migration_profit_trail_forced_v1"] = True
        if forced_count > 0:
            log.info(f"🔧 Migration ponctuelle : profit_trail_enabled forcé à True sur {forced_count} wallet(s) existant(s).")

    def _migrate_reset_legacy_already_bought(self, state: dict):
        """
        MIGRATION PONCTUELLE (demande explicite, 19 août 2026) : buy_only_once
        bloquait avant TOUT nouvel achat d'un wallet dès son premier succès,
        pour toujours (confirmé voulu à l'origine — voir l'ancien commentaire
        "même s'il rachète le même token" dans config.py) ; maintenant, ne
        bloque que le rachat d'un token déjà acheté (voir mark_bought/
        has_already_bought ci-dessus). Les wallets qui avaient déjà
        "already_bought": True avant ce changement resteraient bloqués sur
        TOUT (le repli de compatibilité de has_already_bought traite "on ne
        sait pas quel token" comme "bloqué partout") — pas le comportement
        voulu. Réinitialise ici "bought_tokens" à une liste VIDE pour ces
        wallets, UNE SEULE FOIS (flag state["_migration_legacy_bought_reset_v1"])
        — ils redeviennent immédiatement éligibles pour copier n'importe quel
        nouveau token, comme s'ils n'avaient jamais rien acheté. On perd la
        trace de LEUR ancien achat précis (l'ancien design ne gardait que
        "oui/non", pas le token), mais c'est sans conséquence : le but est
        seulement d'éviter un futur RACHAT du même token, et cet ancien achat
        est déjà terminé (position fermée depuis, confirmé plus tôt ce soir).
        """
        if state.get("_migration_legacy_bought_reset_v1"):
            return
        wallets = state.get("monitored_dev_wallets", {})
        reset_count = 0
        for address, entry in wallets.items():
            if entry.get("already_bought") and "bought_tokens" not in entry:
                entry["bought_tokens"] = []
                reset_count += 1
        state["_migration_legacy_bought_reset_v1"] = True
        if reset_count > 0:
            log.info(f"🔧 Migration ponctuelle : statut buy_only_once réinitialisé sur {reset_count} wallet(s) existant(s) (passage au blocage par token).")

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
            # AJOUTÉ (demande explicite, 19 août) : critères de qualité pour
            # l'évaluation automatique d'un nouveau dev détecté (voir
            # main._evaluate_new_dev) — avant figés en dur (config.py +
            # valeurs codées en dur dans main.py), maintenant modifiables
            # depuis Telegram, avec un interrupteur pour tout désactiver
            # d'un coup.
            "auto_detection_settings": dict(config.DEFAULT_AUTO_DETECTION_SETTINGS),
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
                        mode: str = "track_creation", settings: dict = None, linked_to_parent: str = None):
        """
        mode: "track_creation" (sniping de dev — défaut) ou "track_buy" (copy trade, hors scope ici)
        settings: dict partiel qui override config.DEFAULT_WALLET_SETTINGS

        linked_to_parent : AJOUTÉ (demande explicite) — adresse du wallet
        "parent" si ce wallet est une adresse de retrait détectée
        automatiquement (voir main.on_wallet_withdrawal). Toujours surveillé
        normalement (souscriptions, détection achat/vente inchangées), mais
        n'apparaît plus comme ligne séparée dans le menu Ruggeurs/Copy
        Trading (voir show_ruggers_menu/show_copytrade_menu) — le compteur
        s'affiche sur la ligne du parent à la place. None (défaut) = wallet
        normal, visible comme avant.
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
            "bought_tokens": [],  # MODIFIÉ (demande explicite, 19 août) : liste des tokens déjà achetés pour buy_only_once — avant, "already_bought": bool bloquait TOUT nouvel achat de ce wallet pour toujours dès le premier succès (confirmé voulu à l'origine, mais pas ce que Cheicki veut réellement) ; maintenant, ne bloque que le RACHAT d'un token déjà acheté, pas un nouveau token différent.
            "added_at": now,
            "last_activity_at": now,
            "linked_to_parent": linked_to_parent,
        }
        self.save()
        if linked_to_parent:
            log.info(f"➕ Adresse de retrait rattachée en arrière-plan à {linked_to_parent[:8]}... : {address[:8]}... (masquée du menu)")
        else:
            log.info(f"➕ Wallet dev ajouté au monitoring : {address[:8]}... ({label}, mode={mode})")

    def count_linked_wallets(self, parent_address: str) -> int:
        """AJOUTÉ (demande explicite) — nombre d'adresses de retrait
        rattachées en arrière-plan à ce wallet parent, pour l'affichage du
        compteur dans le menu (voir add_dev_wallet, linked_to_parent)."""
        return sum(
            1 for entry in self.state.get("monitored_dev_wallets", {}).values()
            if entry.get("linked_to_parent") == parent_address
        )

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

    def get_withdrawal_alert_settings(self) -> dict:
        """AJOUTÉ pour l'alerte retrait SOL — même principe que
        get_cleanup_settings, réglable depuis Telegram sans redéployer."""
        defaults = {
            "enabled": config.WITHDRAWAL_ALERT_ENABLED,
            "min_sol": config.WITHDRAWAL_ALERT_MIN_SOL,
            "min_usdc": config.WITHDRAWAL_ALERT_MIN_USDC,
            "auto_add": config.WITHDRAWAL_ALERT_AUTO_ADD,
            "allow_cascade": config.WITHDRAWAL_ALERT_ALLOW_CASCADE,
        }
        stored = self.state.get("withdrawal_alert_settings", {})
        return {**defaults, **stored}

    def set_withdrawal_alert_settings(self, **kwargs):
        """AJOUTÉ pour l'alerte retrait SOL — met à jour enabled, min_sol,
        auto_add et/ou allow_cascade."""
        settings = self.state.setdefault("withdrawal_alert_settings", {})
        settings.update({k: v for k, v in kwargs.items() if v is not None})
        self.save()

    def get_dev_transfer_settings(self) -> dict:
        """AJOUTÉ pour le système à 90% (transferts SOL importants d'un
        dev) — même principe, réglable depuis Telegram."""
        defaults = {
            "pct_threshold": config.DEV_SOL_TRANSFER_ALERT_PCT,
            "auto_add": config.DEV_TRANSFER_AUTO_ADD,
            "allow_cascade": config.DEV_TRANSFER_ALLOW_CASCADE,
        }
        stored = self.state.get("dev_transfer_settings", {})
        return {**defaults, **stored}

    def set_dev_transfer_settings(self, **kwargs):
        """AJOUTÉ pour le système à 90% — met à jour pct_threshold,
        auto_add et/ou allow_cascade."""
        settings = self.state.setdefault("dev_transfer_settings", {})
        settings.update({k: v for k, v in kwargs.items() if v is not None})
        self.save()

    def check_and_update_auto_add_cooldown(self, source_wallet: str) -> bool:
        """
        AJOUTÉ suite à un vrai cas observé : un wallet source très actif
        (bot/service) pouvait déclencher plusieurs ajouts automatiques
        différents en quelques secondes. Limite à 1 ajout automatique par
        wallet SOURCE toutes les config.AUTO_ADD_COOLDOWN_S secondes
        (1h par défaut), peu importe combien de retraits/transferts il
        fait entre-temps.

        Retourne True si l'ajout est autorisé (et enregistre l'horodatage),
        False si le wallet source est encore en cooldown.
        """
        cooldowns = self.state.setdefault("auto_add_cooldowns", {})
        last_add = cooldowns.get(source_wallet, 0)
        now = time.time()
        if now - last_add < config.AUTO_ADD_COOLDOWN_S:
            return False
        cooldowns[source_wallet] = now
        self.save()
        return True

    def remove_dev_wallet(self, address: str):
        self.state["monitored_dev_wallets"].pop(address, None)
        self.save()

    def remove_auto_detected_wallets(self) -> int:
        """
        AJOUTÉ (demande explicite, 19 août) : nettoyage en masse — supprime
        tous les wallets dont le label commence par "Auto-détecté" (ajoutés
        par le pipeline de détection automatique de _evaluate_new_dev, PAS
        ajoutés manuellement par Cheicki). Ne touche à AUCUN wallet ajouté
        via "➕ Add Rugger"/"➕ Add Copy Trade" ni via un ajout rapide depuis
        une analyse ("➕ Ajouter en Ruggeur/Copy Trading"). Retourne le
        nombre de wallets supprimés.
        """
        wallets = self.state.get("monitored_dev_wallets", {})
        to_remove = [addr for addr, entry in wallets.items() if entry.get("label", "").startswith("Auto-détecté")]
        for addr in to_remove:
            wallets.pop(addr, None)
        if to_remove:
            self.save()
        return len(to_remove)

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

    def mark_bought(self, address: str, token_mint: str):
        """
        MODIFIÉ (demande explicite, 19 août) : marque désormais QUE ce
        token précis comme acheté pour ce wallet, pas "ce wallet a acheté,
        point final". Rétrocompatible avec les anciennes entrées qui
        avaient encore "bought_tokens" absent (créées avant ce changement) —
        setdefault crée la liste au premier appel plutôt que de planter.
        """
        entry = self.state["monitored_dev_wallets"].get(address)
        if entry:
            entry.setdefault("bought_tokens", []).append(token_mint)
            self.save()

    def has_already_bought(self, address: str, token_mint: str) -> bool:
        """
        MODIFIÉ (demande explicite, 19 août) : vérifie maintenant si CE
        token précis a déjà été acheté pour ce wallet — pas "ce wallet
        a-t-il déjà acheté N'IMPORTE QUOI". Avant ce changement, un wallet
        qui avait réussi UN SEUL achat devenait bloqué pour toujours, même
        sur des tokens totalement différents plus tard — pas le comportement
        voulu (confirmé explicitement). Repli sur l'ancien champ
        "already_bought" (bool) pour les wallets pas encore migrés vers
        "bought_tokens" — traité comme "a déjà acheté quelque chose, mais
        on ne sait pas quoi" plutôt que de perdre l'information silencieusement.
        """
        entry = self.state["monitored_dev_wallets"].get(address, {})
        if "bought_tokens" in entry:
            return token_mint in entry["bought_tokens"]
        return entry.get("already_bought", False)  # ancienne donnée, avant la migration

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
                               preset_name: str = None, parent_address: str = None,
                               ranges: list = None):
        """
        target_type: "exchange" (montant fixe) ou "mere" (tous les transferts sortants)
        parent_address: si défini, les nouvelles adresses enfants héritent selon le
                        toggle inherit_settings du parent (voir _resolve_child_settings
                        dans protection_scanner.py)
        ranges: AJOUTÉ (demande explicite, 19 août) — liste de {"min": float,
                "max": float}, stockée DIRECTEMENT sur la cible plutôt que de devoir
                passer par un parent_address existant avec transfer_ranges configuré
                sur ses propres settings (l'ancien seul chemin possible). Permet
                d'ajouter un wallet intermédiaire avec son intervalle en UNE étape,
                sans wallet parent préexistant. Voir protection_scanner._matches_target_criteria
                et _resolve_child_settings, qui vérifient maintenant target["ranges"]
                en premier avant de retomber sur le mécanisme parent_address.
        """
        self.state["protection_targets"][label] = {
            "type": target_type,
            "address": address,
            "amount_sol": amount_sol,
            "tolerance": tolerance or config.FIXED_AMOUNT_TOLERANCE_SOL,
            "preset_name": preset_name,
            "parent_address": parent_address,
            "ranges": ranges,
            "known_recipients": [],  # évite de re-traiter les mêmes adresses à chaque scan
        }
        self.save()
        log.info(f"➕ Protection ajoutée : {label} ({target_type}, {address[:8]}...)")

    # ══════════════════════════════════════════════════════════
    # DEVS BLOQUÉS (Copy Trading) — INDIVIDUEL PAR WALLET
    # ══════════════════════════════════════════════════════════
    # MODIFIÉ (demande explicite, 19 août) : d'abord une liste globale
    # partagée par tous les wallets, puis précisé — chaque wallet Copy
    # Trading a maintenant SA PROPRE liste (stockée dans ses settings,
    # comme buy_only_once ou profit_trail_enabled), indépendante des autres
    # wallets. Bloquer un dev sur un wallet n'affecte que CE wallet précis.
    # Un dev bloqué n'a toujours pas besoin d'être lui-même un wallet suivi
    # par le bot — voir main.on_copytrade_buy pour l'application réelle.

    def add_blocked_dev(self, wallet_address: str, dev_address: str, label: str = None):
        import time
        settings = self.get_wallet_settings(wallet_address)
        blocked = settings.setdefault("blocked_devs", {})
        blocked[dev_address] = {"label": label or dev_address[:8] + "...", "added_at": time.time()}
        self.save()
        log.info(f"🚫 Dev bloqué ajouté sur {wallet_address[:8]}... : {dev_address[:8]}...")

    def remove_blocked_dev(self, wallet_address: str, dev_address: str):
        settings = self.get_wallet_settings(wallet_address)
        settings.get("blocked_devs", {}).pop(dev_address, None)
        self.save()
        log.info(f"✅ Dev débloqué sur {wallet_address[:8]}... : {dev_address[:8]}...")

    def is_dev_blocked(self, wallet_address: str, dev_address: str) -> bool:
        settings = self.get_wallet_settings(wallet_address)
        return dev_address in settings.get("blocked_devs", {})

    def list_blocked_devs(self, wallet_address: str) -> dict:
        settings = self.get_wallet_settings(wallet_address)
        return settings.get("blocked_devs", {})

    def list_all_blocked_devs_aggregated(self) -> dict:
        """
        AJOUTÉ (demande explicite, 19 août) : vue d'ensemble de tous les
        devs bloqués, tous wallets Copy Trading confondus — les données
        restent stockées PAR wallet (voir list_blocked_devs), ceci ne fait
        qu'agréger pour l'affichage. Retourne {dev_address: {"label":,
        "wallet_count": int, "wallet_labels": [str, ...]}}.
        """
        aggregated = {}
        for address, entry in self.state.get("monitored_dev_wallets", {}).items():
            if entry.get("mode") not in ("track_buy", "track_sell"):
                continue
            wallet_label = entry.get("label", address[:8] + "...")
            for dev_address, info in entry.get("settings", {}).get("blocked_devs", {}).items():
                agg = aggregated.setdefault(dev_address, {"label": info.get("label", dev_address[:8] + "..."), "wallet_count": 0, "wallet_labels": []})
                agg["wallet_count"] += 1
                agg["wallet_labels"].append(wallet_label)
        return aggregated

    def remove_blocked_dev_everywhere(self, dev_address: str):
        """
        AJOUTÉ (demande explicite, 19 août) : retire ce dev de la liste des
        devs bloqués de TOUS les wallets Copy Trading où il apparaît —
        pendant symétrique de add_blocked_dev_all_wallets côté suppression.
        """
        removed_count = 0
        for address, entry in self.state.get("monitored_dev_wallets", {}).items():
            blocked = entry.get("settings", {}).get("blocked_devs", {})
            if dev_address in blocked:
                del blocked[dev_address]
                removed_count += 1
        if removed_count:
            self.save()
        return removed_count

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
