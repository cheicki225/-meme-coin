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
import hashlib
import glob
import json
import logging
import math
import os
import shutil
import tempfile
import threading
import time

import config
from sqlite_state import (
    SQLiteStateStore,
    StateStoreConflictError,
    StateStoreCorruptError,
    StateStoreError,
)

BACKUP_DIR_NAME = "backups"
BACKUP_INTERVAL_S = 300   # 5 minutes entre deux backups automatiques
MAX_BACKUPS = 30          # rotation — garde les 30 dernières, supprime les plus vieilles
MAX_CONFIRMED_ACCOUNTING_RECORDS = 1000


def _accounting_fingerprint(accounting: dict) -> str:
    """Return a stable digest for the complete accounting evidence object."""
    try:
        canonical = json.dumps(
            accounting, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Confirmed accounting evidence must be finite JSON data") from exc
    return hashlib.sha256(canonical.encode("utf-8", errors="strict")).hexdigest()


def _accounting_signature_key(signature: str) -> str:
    """Hash signatures in the all-time idempotency index."""
    return hashlib.sha256(signature.encode("utf-8", errors="strict")).hexdigest()

log = logging.getLogger("monitoring_list")


class DataStore:
    """Charge/sauvegarde l'état complet du bot dans un fichier JSON."""

    def __init__(self, path: str | None = None):
        self.path = path or config.DATA_FILE
        self._use_sqlite = path is None and getattr(config, "DATA_BACKEND", "json") == "sqlite"
        db_path = config.SQLITE_DB_FILE if self._use_sqlite else self.path
        self._backup_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)) or ".", BACKUP_DIR_NAME)
        self._last_backup_time = 0
        self._confirmed_accounting_lock = threading.RLock()
        self._sqlite_store = None
        self._state_revision: int | None = None
        if self._use_sqlite:
            try:
                self._sqlite_store = SQLiteStateStore(config.SQLITE_DB_FILE)
            except StateStoreCorruptError:
                recovered = False
                for candidate in self._backup_candidates("sniper_state_*.sqlite3"):
                    try:
                        backup_store = SQLiteStateStore(candidate)
                        if not backup_store.has_committed_state():
                            continue
                        backup_store.load(self._default_state)
                        backup_store.backup_to(config.SQLITE_DB_FILE)
                        self._sqlite_store = SQLiteStateStore(config.SQLITE_DB_FILE)
                        recovered = True
                        log.warning("SQLite state restored from backup %s", candidate)
                        break
                    except (StateStoreError, OSError) as exc:
                        log.error("SQLite backup %s is not recoverable: %s", candidate, exc)
                if not recovered:
                    raise
        self.state = self._load()
        if self._use_sqlite and os.path.exists(config.SQLITE_DB_FILE):
            self._create_backup(reason="startup")
        elif os.path.exists(self.path):
            self._create_backup(reason="startup")

    def _backup_candidates(self, pattern: str) -> list[str]:
        candidates = glob.glob(os.path.join(self._backup_dir, pattern))
        return sorted(candidates, key=lambda item: os.path.getmtime(item), reverse=True)

    def _load(self) -> dict:
        loaded = None
        if self._sqlite_store:
            try:
                loaded = self._sqlite_store.load(self._default_state, legacy_json_path=self.path)
                self._state_revision = self._sqlite_store.last_loaded_revision
            except StateStoreError as exc:
                if (not isinstance(exc, StateStoreCorruptError)
                        and self._sqlite_store.has_committed_state()):
                    # Never downgrade a database written by a newer schema.
                    raise
                if isinstance(exc, StateStoreCorruptError):
                    for candidate in self._backup_candidates("sniper_state_*.sqlite3"):
                        try:
                            backup_store = SQLiteStateStore(candidate)
                            if not backup_store.has_committed_state():
                                continue
                            restored = backup_store.load(self._default_state)
                            expected_revision = self._sqlite_store.last_loaded_revision
                            if expected_revision is None:
                                raise StateStoreConflictError(
                                    "Cannot restore SQLite state without a verified revision"
                                )
                            self._state_revision = self._sqlite_store.save(
                                restored, expected_revision=expected_revision
                            )
                            loaded = restored
                            log.warning("State restored from SQLite backup %s after load failure: %s", candidate, exc)
                            break
                        except (StateStoreError, OSError) as backup_error:
                            log.error("SQLite backup %s is not recoverable: %s", candidate, backup_error)
                if loaded is None:
                    # A stale legacy JSON snapshot must never replace a
                    # previously committed SQLite state. It may predate a
                    # signed, still-pending transaction or emergency stop.
                    if self._sqlite_store.has_committed_state():
                        raise StateStoreCorruptError(
                            "Committed SQLite state could not be recovered; refusing legacy JSON downgrade"
                        ) from exc
                    for candidate in self._backup_candidates("sniper_data_*.json"):
                        try:
                            with open(candidate, "r", encoding="utf-8") as source:
                                restored = json.load(source)
                            if isinstance(restored, dict):
                                loaded = restored
                                expected_revision = self._sqlite_store.last_loaded_revision
                                if expected_revision is None:
                                    raise StateStoreConflictError(
                                        "Cannot import JSON backup without a verified SQLite revision"
                                    )
                                self._state_revision = self._sqlite_store.save(
                                    loaded, expected_revision=expected_revision
                                )
                                log.warning("State imported from JSON backup %s after load failure: %s", candidate, exc)
                                break
                        except (OSError, json.JSONDecodeError, StateStoreError) as backup_error:
                            log.error("JSON backup %s is not recoverable: %s", candidate, backup_error)
                if loaded is None:
                    raise
        elif os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as source:
                    loaded = json.load(source)
                if not isinstance(loaded, dict):
                    raise StateStoreError("JSON state root must be an object")
            except (json.JSONDecodeError, OSError, StateStoreError) as exc:
                backup_dir = os.path.join(os.path.dirname(os.path.abspath(self.path)), BACKUP_DIR_NAME)
                for candidate in sorted(glob.glob(os.path.join(backup_dir, "sniper_data_*.json")), reverse=True):
                    try:
                        with open(candidate, "r", encoding="utf-8") as source:
                            restored = json.load(source)
                        if isinstance(restored, dict):
                            loaded = restored
                            log.warning("State restored from JSON backup %s after load failure: %s", candidate, exc)
                            break
                    except (OSError, json.JSONDecodeError) as backup_error:
                        log.error("JSON backup %s is not recoverable: %s", candidate, backup_error)
                if loaded is None:
                    raise StateStoreError(f"State file is unreadable and no valid backup exists: {self.path}") from exc
        else:
            loaded = self._default_state()

        default = self._default_state()
        for key, value in default.items():
            loaded.setdefault(key, value)
        self._backfill_wallet_settings(loaded)
        self._migrate_force_buy_only_once(loaded)
        self._migrate_force_profit_trail_enabled(loaded)
        self._migrate_reset_legacy_already_bought(loaded)
        return loaded

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
            "emergency_stop": True,
            "emergency_stop_reason": "startup safety default",
            "emergency_stop_at": None,
            "pending_transactions": [],
            "resolved_transactions": [],
            "unapplied_failed_transaction_fees_lamports": 0,
            "confirmed_accounting_ledger": [],
            # Compact SHA-256 tombstones preserve idempotency after ledger-row
            # eviction. The index shares the same atomic state save as the rows.
            "confirmed_accounting_idempotency": {},
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
        try:
            if self._sqlite_store:
                self._state_revision = self._sqlite_store.save(
                    self.state, expected_revision=self._state_revision
                )
            else:
                parent_dir = os.path.dirname(os.path.abspath(self.path))
                os.makedirs(parent_dir, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=parent_dir,
                    prefix=".sniper-state-", suffix=".tmp", delete=False,
                ) as temporary:
                    json.dump(self.state, temporary, indent=2, ensure_ascii=False, allow_nan=False)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    temporary_path = temporary.name
                os.replace(temporary_path, self.path)
        except Exception as exc:
            log.error("State persistence failed: %s", exc)
            raise

        now = time.time()
        if now - self._last_backup_time >= BACKUP_INTERVAL_S:
            self._create_backup(reason="periodic")
            self._last_backup_time = now

    def _create_backup(self, reason: str = "periodic"):
        """Create a consistent SQLite snapshot or atomic legacy JSON backup."""
        try:
            os.makedirs(self._backup_dir, exist_ok=True)
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            if self._sqlite_store:
                backup_path = os.path.join(self._backup_dir, f"sniper_state_{timestamp}_{reason}.sqlite3")
                self._sqlite_store.backup_to(backup_path)
            else:
                if not os.path.exists(self.path):
                    return
                backup_path = os.path.join(self._backup_dir, f"sniper_data_{timestamp}_{reason}.json")
                temporary = backup_path + ".tmp"
                shutil.copy2(self.path, temporary)
                os.replace(temporary, backup_path)
            self._prune_old_backups()
            log.debug("State backup created: %s", backup_path)
        except Exception as exc:
            log.error("State backup failed: %s", exc)

    def _prune_old_backups(self):
        try:
            patterns = ("sniper_data_*.json", "sniper_state_*.sqlite3")
            backups = [
                path for pattern in patterns for path in glob.glob(os.path.join(self._backup_dir, pattern))
            ]
            backups.sort(key=lambda item: os.path.getmtime(item))
            while len(backups) > MAX_BACKUPS:
                os.remove(backups.pop(0))
        except Exception as exc:
            log.error("Backup rotation failed: %s", exc)

    def list_backups(self) -> list:
        """Return JSON and SQLite backups, newest first."""
        paths = []
        for pattern in ("sniper_state_*.sqlite3", "sniper_data_*.json"):
            paths.extend(glob.glob(os.path.join(self._backup_dir, pattern)))
        return sorted(paths, key=lambda item: os.path.getmtime(item), reverse=True)

    def restore_from_backup(self, backup_path: str) -> bool:
        """Restore a JSON or SQLite backup, preserving the current state first."""
        if not os.path.exists(backup_path):
            log.error("Backup not found: %s", backup_path)
            return False
        try:
            self._create_backup(reason="pre_restore")
            if backup_path.lower().endswith((".sqlite3", ".db")):
                restored_state = SQLiteStateStore(backup_path).load(self._default_state)
            else:
                with open(backup_path, "r", encoding="utf-8") as source:
                    restored_state = json.load(source)
            if not isinstance(restored_state, dict):
                raise StateStoreError("Backup root must be an object")
            self.state = restored_state
            for key, value in self._default_state().items():
                self.state.setdefault(key, value)
            self.save()
            log.info("State restored from backup %s", os.path.basename(backup_path))
            return True
        except Exception as exc:
            log.error("Restore from backup %s failed: %s", backup_path, exc)
            return False

    def activate_emergency_stop(self, reason: str) -> None:
        self.state["emergency_stop"] = True
        self.state["emergency_stop_reason"] = str(reason)[:500]
        self.state["emergency_stop_at"] = int(time.time())
        self.save()

    def clear_emergency_stop(self) -> bool:
        pending = self.state.get("pending_transactions", [])
        if not isinstance(pending, list) or pending:
            return False
        missing = object()
        previous = {
            key: self.state.get(key, missing)
            for key in ("emergency_stop", "emergency_stop_reason", "emergency_stop_at")
        }
        self.state["emergency_stop"] = False
        self.state["emergency_stop_reason"] = None
        self.state["emergency_stop_at"] = None
        try:
            self.save()
        except Exception:
            # A failed persistence must never leave the in-memory trader
            # running with the emergency stop cleared.
            for key, value in previous.items():
                if value is missing:
                    self.state.pop(key, None)
                else:
                    self.state[key] = value
            raise
        return True

    def record_pending_transaction(self, record: dict) -> None:
        signature = record.get("signature")
        if not signature:
            raise ValueError("pending transaction signature is required")
        created_collection = "pending_transactions" not in self.state
        pending = self.state.setdefault("pending_transactions", [])
        added = not any(item.get("signature") == signature for item in pending)
        if added:
            pending.append(dict(record))
        try:
            self.save()
        except Exception:
            # The signed transaction is not broadcast when this callback
            # fails. Keep memory aligned with durable state so a later retry
            # cannot mistake an unpersisted signature for a submitted swap.
            if added:
                pending.pop()
            if created_collection:
                self.state.pop("pending_transactions", None)
            raise

    def mark_pending_transaction_accounted(self, signature: str, accounting: dict) -> bool:
        for item in self.state.setdefault("pending_transactions", []):
            if item.get("signature") == signature:
                previous_status = item.get("status")
                had_accounting = "accounting" in item
                previous_accounting = item.get("accounting")
                item["status"] = "accounted"
                item["accounting"] = dict(accounting)
                try:
                    self.save()
                except Exception:
                    if previous_status is None:
                        item.pop("status", None)
                    else:
                        item["status"] = previous_status
                    if had_accounting:
                        item["accounting"] = previous_accounting
                    else:
                        item.pop("accounting", None)
                    raise
                return True
        return False

    def mark_pending_transaction_unknown(self, signature: str) -> bool:
        """Persist an unresolved transaction as unknown for manual reconciliation."""
        pending = self.state.get("pending_transactions", [])
        if not isinstance(pending, list) or any(not isinstance(item, dict) for item in pending):
            raise ValueError("pending_transactions must be a list of objects")

        for item in pending:
            if item.get("signature") != signature:
                continue
            missing = object()
            previous_status = item.get("status", missing)
            item["status"] = "unknown"
            try:
                self.save()
            except Exception:
                if previous_status is missing:
                    item.pop("status", None)
                else:
                    item["status"] = previous_status
                raise
            return True
        return False

    def resolve_pending_transaction(self, signature: str, outcome: dict | None = None) -> bool:
        pending = self.state.get("pending_transactions", [])
        if not isinstance(pending, list) or any(not isinstance(item, dict) for item in pending):
            raise ValueError("pending_transactions must be a list of objects")
        index = next((i for i, item in enumerate(pending) if item.get("signature") == signature), None)
        if index is None:
            return False
        resolved_outcome = dict(outcome or {})
        is_finalized_failure = resolved_outcome.get("chain_status") == "finalized_failed"
        if is_finalized_failure:
            fee = resolved_outcome.get("fee_lamports")
            total_fees = self.state.get("unapplied_failed_transaction_fees_lamports", 0)
            if (not isinstance(fee, int) or isinstance(fee, bool) or fee < 0
                    or not isinstance(total_fees, int) or isinstance(total_fees, bool) or total_fees < 0):
                raise ValueError("Finalized failed transaction requires valid fee accounting")

        history_exists = "resolved_transactions" in self.state
        history = self.state.get("resolved_transactions", [])
        if not isinstance(history, list):
            raise ValueError("resolved_transactions must be a list")
        match = pending[index]
        resolved = dict(match)
        resolved["outcome"] = resolved_outcome
        resolved["resolved_at"] = int(time.time())
        previous_history = list(history)
        fees_existed = "unapplied_failed_transaction_fees_lamports" in self.state
        previous_fees = self.state.get("unapplied_failed_transaction_fees_lamports", 0)
        if is_finalized_failure:
            self.state["unapplied_failed_transaction_fees_lamports"] = previous_fees + fee
        try:
            pending.pop(index)
            history.append(resolved)
            del history[:-500]
            if not history_exists:
                self.state["resolved_transactions"] = history
            self.save()
        except Exception:
            pending.insert(index, match)
            if history_exists:
                history[:] = previous_history
            else:
                self.state.pop("resolved_transactions", None)
            if is_finalized_failure:
                if fees_existed:
                    self.state["unapplied_failed_transaction_fees_lamports"] = previous_fees
                else:
                    self.state.pop("unapplied_failed_transaction_fees_lamports", None)
            raise
        return True

    def record_confirmed_accounting(
        self,
        signature: str,
        accounting: dict,
        valuation: dict,
    ) -> bool:
        """Persist one confirmed accounting result without changing PAPER PnL.

        The bounded ledger is an auditable source record for confirmed wallet
        cash flows and fees. It is deliberately separate from simulated PAPER
        positions, totals, and risk counters. Repeating an identical record is
        idempotent; conflicting evidence for the same signature is rejected.
        An unknown valuation may be reconciled once with a valued result when
        the underlying confirmed accounting data is unchanged. A compact,
        all-time digest index is saved atomically beside the bounded detail rows.
        """
        lock = getattr(self, "_confirmed_accounting_lock", None)
        if lock is None:
            # The normal constructor creates this lock. This fallback supports
            # isolated test doubles that construct DataStore without __init__.
            lock = threading.RLock()
            self._confirmed_accounting_lock = lock
        with lock:
            for attempt in range(3):
                try:
                    return self._record_confirmed_accounting_locked(
                        signature, accounting, valuation
                    )
                except StateStoreConflictError:
                    if self._sqlite_store is None or attempt == 2:
                        raise
                    # A different process committed since this snapshot was
                    # loaded. Reapply this ledger event to the latest state;
                    # never overwrite that process with a stale document.
                    self.state = self._load()
            raise StateStoreConflictError(
                "Confirmed accounting could not be committed after reload retries"
            )

    def _record_confirmed_accounting_locked(
        self,
        signature: str,
        accounting: dict,
        valuation: dict,
    ) -> bool:
        """Validate, deduplicate, and atomically persist one accounting event."""
        if not isinstance(signature, str) or not signature.strip():
            raise ValueError("Confirmed accounting signature is required")
        if len(signature) > 256:
            raise ValueError("Confirmed accounting signature is too long")
        if not isinstance(accounting, dict) or not isinstance(valuation, dict):
            raise ValueError("Confirmed accounting and valuation must be objects")
        fingerprint = _accounting_fingerprint(accounting)
        signature_key = _accounting_signature_key(signature)
        from transaction_accounting import VALUATION_SCHEMA_VERSION, value_confirmed_sol_cashflow_usd

        if accounting.get("accounting_source") != "confirmed_rpc_transaction_meta":
            raise ValueError("Only confirmed RPC accounting can enter this ledger")
        if type(accounting.get("accounting_schema_version")) is not int or accounting["accounting_schema_version"] != 1:
            raise ValueError("Unsupported confirmed accounting schema version")
        native_delta = accounting.get("native_delta_lamports")
        fee = accounting.get("fee_lamports")
        if type(native_delta) is not int or type(fee) is not int or fee < 0:
            raise ValueError("Confirmed SOL delta and fee must be integer lamports")
        if valuation.get("accounting_source") != "confirmed_rpc_transaction_meta":
            raise ValueError("Valuation source does not match confirmed accounting")
        if (
            type(valuation.get("valuation_schema_version")) is not int
            or valuation["valuation_schema_version"] != VALUATION_SCHEMA_VERSION
        ):
            raise ValueError("Unsupported confirmed valuation schema version")
        expected_valuation = value_confirmed_sol_cashflow_usd(
            accounting,
            sol_usd_rate=valuation.get("sol_usd_rate"),
            rate_timestamp=valuation.get("sol_usd_rate_timestamp"),
            rate_source=valuation.get("sol_usd_rate_source"),
            rate_verified=valuation.get(
                "sol_usd_rate_caller_attested", valuation.get("sol_usd_rate_verified")
            ),
            max_rate_age_s=valuation.get("max_rate_age_s"),
        )
        if valuation != expected_valuation:
            raise ValueError("Valuation does not match confirmed deltas and rate evidence")
        if valuation.get("fee_currency") != "SOL" or valuation.get("fee_included_in_sol_cashflow") is not True:
            raise ValueError("Confirmed valuation is missing fee/cash-flow semantics")
        status = valuation.get("valuation_status")
        if status not in {"valued", "unknown"}:
            raise ValueError("Confirmed valuation status must be valued or unknown")
        if status == "valued":
            for field in ("sol_cashflow_usd", "fee_usd", "sol_usd_rate", "sol_usd_rate_timestamp"):
                value = valuation.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"Valued record requires numeric {field}")
                try:
                    finite_value = math.isfinite(float(value))
                except (OverflowError, ValueError):
                    finite_value = False
                if not finite_value:
                    raise ValueError(f"Valued record requires finite {field}")
            if valuation.get("sol_usd_rate", 0) <= 0:
                raise ValueError("Valued record requires a positive SOL/USD rate")
            if not isinstance(valuation.get("sol_usd_rate_source"), str) or not valuation["sol_usd_rate_source"].strip():
                raise ValueError("Valued record requires SOL/USD source provenance")
        elif valuation.get("sol_cashflow_usd") is not None or valuation.get("fee_usd") is not None:
            raise ValueError("Unknown valuation must not contain invented USD amounts")

        record = {
            "signature": signature,
            "accounting_fingerprint": fingerprint,
            "accounting_schema_version": accounting.get("accounting_schema_version"),
            "valuation_schema_version": valuation.get("valuation_schema_version"),
            "accounting_source": "confirmed_rpc_transaction_meta",
            "native_delta_lamports": native_delta,
            "fee_lamports": fee,
            "fee_status": valuation.get("fee_status", "confirmed"),
            "fee_currency": "SOL",
            "fee_included_in_sol_cashflow": True,
            "accounting_domain": "CONFIRMED_CHAIN",
            "applied_to_paper_pnl": False,
            "event_timestamp": valuation.get("event_timestamp"),
            "valuation_status": status,
            "valuation_reason": valuation.get("valuation_reason"),
            "sol_cashflow_usd": valuation.get("sol_cashflow_usd"),
            "fee_usd": valuation.get("fee_usd"),
            "sol_usd_rate": valuation.get("sol_usd_rate"),
            "sol_usd_rate_timestamp": valuation.get("sol_usd_rate_timestamp"),
            "sol_usd_rate_source": valuation.get("sol_usd_rate_source"),
            "sol_usd_rate_verified": valuation.get("sol_usd_rate_verified"),
            "sol_usd_rate_caller_attested": valuation.get("sol_usd_rate_caller_attested"),
            "rate_origin_status": valuation.get("rate_origin_status"),
            "max_rate_age_s": valuation.get("max_rate_age_s"),
            "rate_age_seconds": valuation.get("rate_age_seconds"),
        }
        ledger_existed = "confirmed_accounting_ledger" in self.state
        original_ledger = self.state.get("confirmed_accounting_ledger", [])
        if not isinstance(original_ledger, list) or any(not isinstance(item, dict) for item in original_ledger):
            raise ValueError("Confirmed accounting ledger must be a list of objects")
        working_ledger = list(original_ledger)
        index = next((i for i, item in enumerate(working_ledger) if item.get("signature") == signature), None)

        idempotency_existed = "confirmed_accounting_idempotency" in self.state
        original_idempotency = self.state.get("confirmed_accounting_idempotency", {})
        if not isinstance(original_idempotency, dict):
            raise ValueError("Confirmed accounting idempotency index must be an object")
        working_idempotency = dict(original_idempotency)
        for key, value in working_idempotency.items():
            if (
                not isinstance(key, str)
                or len(key) != 64
                or any(character not in "0123456789abcdef" for character in key)
                or (value is not None and (
                    not isinstance(value, str)
                    or len(value) != 64
                    or any(character not in "0123456789abcdef" for character in value)
                ))
            ):
                raise ValueError("Confirmed accounting idempotency index is malformed")
        idempotency_changed = False

        # Backfill tombstones for all rows retained by older versions before a
        # future append can evict them. Legacy rows without a complete evidence
        # fingerprint receive an identity-only tombstone until replayed while
        # their detailed row is still retained.
        for item in working_ledger:
            old_signature = item.get("signature")
            if not isinstance(old_signature, str) or not old_signature.strip():
                raise ValueError("Confirmed accounting ledger contains a malformed signature")
            old_key = _accounting_signature_key(old_signature)
            old_fingerprint = item.get("accounting_fingerprint")
            if old_fingerprint is not None and (
                not isinstance(old_fingerprint, str)
                or len(old_fingerprint) != 64
                or any(character not in "0123456789abcdef" for character in old_fingerprint)
            ):
                raise ValueError("Confirmed accounting ledger contains a malformed fingerprint")
            if old_key not in working_idempotency:
                working_idempotency[old_key] = old_fingerprint
                idempotency_changed = True
            elif old_fingerprint is not None:
                indexed_fingerprint = working_idempotency[old_key]
                if indexed_fingerprint not in (None, old_fingerprint):
                    raise ValueError("Confirmed accounting ledger conflicts with its idempotency index")
                if indexed_fingerprint is None:
                    working_idempotency[old_key] = old_fingerprint
                    idempotency_changed = True
            elif working_idempotency[old_key] is not None:
                raise ValueError("Legacy confirmed accounting row conflicts with a full fingerprint")

        indexed_fingerprint = working_idempotency.get(signature_key, "__missing__")
        if indexed_fingerprint not in ("__missing__", None, fingerprint):
            raise ValueError("Conflicting confirmed accounting for signature")
        if index is None and indexed_fingerprint != "__missing__":
            # The detailed row may have been evicted already. Its permanent
            # tombstone still blocks a replay from adding it a second time.
            if idempotency_changed:
                self.state["confirmed_accounting_idempotency"] = working_idempotency
                try:
                    self.save()
                except Exception:
                    if idempotency_existed:
                        self.state["confirmed_accounting_idempotency"] = original_idempotency
                    else:
                        self.state.pop("confirmed_accounting_idempotency", None)
                    raise
            return False

        if indexed_fingerprint == "__missing__":
            working_idempotency[signature_key] = fingerprint
            idempotency_changed = True

        ledger_changed = False
        if index is not None:
            previous = working_ledger[index]
            immutable_fields = (
                "signature", "accounting_source", "native_delta_lamports", "fee_lamports",
                "event_timestamp",
            )
            if any(previous.get(field) != record.get(field) for field in immutable_fields):
                raise ValueError("Conflicting confirmed accounting for signature")
            previous_fingerprint = previous.get("accounting_fingerprint")
            if previous_fingerprint is None:
                # Historical rows did not retain all fields needed to prove
                # that a replay has the same wallet, mints, and token deltas.
                # Keep an identity-only tombstone and prohibit reconciliation.
                if idempotency_changed:
                    self.state["confirmed_accounting_idempotency"] = working_idempotency
                    try:
                        self.save()
                    except Exception:
                        if idempotency_existed:
                            self.state["confirmed_accounting_idempotency"] = original_idempotency
                        else:
                            self.state.pop("confirmed_accounting_idempotency", None)
                        raise
                return False
            if previous_fingerprint != fingerprint:
                raise ValueError("Conflicting confirmed accounting for signature")
            if previous == record:
                pass
            elif previous.get("valuation_status") == "unknown" and status == "unknown":
                # Additional caller claims cannot upgrade or rewrite an
                # unresolved valuation; retain the original UNKNOWN evidence.
                pass
            elif previous.get("valuation_status") != "unknown" or status != "valued":
                raise ValueError("Conflicting valuation for signature")
            else:
                working_ledger[index] = record
                ledger_changed = True
        else:
            working_ledger.append(record)
            del working_ledger[:-MAX_CONFIRMED_ACCOUNTING_RECORDS]
            ledger_changed = True

        if not ledger_changed and not idempotency_changed:
            return False

        self.state["confirmed_accounting_ledger"] = working_ledger
        self.state["confirmed_accounting_idempotency"] = working_idempotency
        try:
            self.save()
        except Exception:
            if ledger_existed:
                self.state["confirmed_accounting_ledger"] = original_ledger
            else:
                self.state.pop("confirmed_accounting_ledger", None)
            if idempotency_existed:
                self.state["confirmed_accounting_idempotency"] = original_idempotency
            else:
                self.state.pop("confirmed_accounting_idempotency", None)
            raise
        return ledger_changed

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
        settings = entry.get("settings")
        if not isinstance(settings, dict):
            settings = copy.deepcopy(config.DEFAULT_WALLET_SETTINGS)
            entry["settings"] = settings
        for key, default_value in config.DEFAULT_WALLET_SETTINGS.items():
            settings.setdefault(key, copy.deepcopy(default_value))
        return settings

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
