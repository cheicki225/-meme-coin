"""
════════════════════════════════════════════════════════════════
PROTECTION SCANNER — Surveillance proactive schéma mère/exchange
════════════════════════════════════════════════════════════════
Reproduit le menu "Protection" de F Project vu dans les transcripts :
au lieu d'attendre qu'un nouveau dev crée un token puis d'évaluer son
historique (latence), on surveille en continu les adresses "mère" ou
"exchange" connues pour repérer les NOUVEAUX wallets qu'elles financent,
et on les ajoute IMMÉDIATEMENT au monitoring — avant même qu'ils aient
créé quoi que ce soit.

Résultat : quand un de ces wallets crée effectivement un token, le
WebSocket listener le reconnaît comme "déjà monitored" (voir main.py)
et déclenche un achat immédiat, sans délai d'évaluation. C'est cette
latence-là que la vidéo appelle "bloc zéro".

Tourne en tâche de fond, toutes les PROTECTION_SCAN_INTERVAL_S secondes.
"""

import asyncio
import copy
import logging

import config
import pattern_detector
import fund_tracer

log = logging.getLogger("protection_scanner")


class ProtectionScanner:
    def __init__(self, data_store, notifier=None):
        self.data_store = data_store
        self.notifier = notifier
        self._running = False

    async def start(self):
        self._running = True
        log.info("🛡️  Protection scanner démarré.")
        while self._running:
            await self._scan_all_targets()
            await asyncio.sleep(config.PROTECTION_SCAN_INTERVAL_S)

    def stop(self):
        self._running = False

    async def _scan_all_targets(self):
        targets = dict(self.data_store.state.get("protection_targets", {}))
        if not targets:
            return

        for label, target in targets.items():
            try:
                await self._scan_one_target(label, target)
            except Exception as e:
                log.error(f"Erreur scan protection '{label}': {e}")

    async def _scan_one_target(self, label: str, target: dict):
        target_type = target["type"]
        address = target["address"]

        # Résout les settings du parent EN AMONT — nécessaire pour connaître
        # transfer_ranges (plusieurs plages) et fresh_wallet_only avant de
        # choisir la méthode de scan et de valider chaque nouveau wallet.
        child_settings = self._resolve_child_settings(target)
        parent_ranges = child_settings.get("transfer_ranges") if child_settings else None

        if target_type == "exchange":
            if parent_ranges:
                # Le parent a configuré plusieurs plages de montant (Transfer Ranges,
                # jusqu'à 3) — on les utilise TOUTES plutôt que le montant fixe unique.
                recipients = await pattern_detector.find_multi_range_recipients(address, parent_ranges)
            else:
                recipients = await pattern_detector.find_fixed_amount_recipients(
                    address, target["amount_sol"], target.get("tolerance"),
                )
        elif target_type == "mere":
            recipients = await pattern_detector.find_all_outgoing_recipients(address)
        else:
            log.warning(f"Type de protection inconnu '{target_type}' pour {label}")
            return

        require_fresh = child_settings.get("fresh_wallet_only", False) if child_settings else False

        new_count = 0
        for r in recipients:
            recipient = r["recipient"]

            if self.data_store.is_recipient_known(label, recipient):
                continue
            self.data_store.mark_recipient_known(label, recipient)

            if self.data_store.is_dev_monitored(recipient):
                continue

            # ── Fresh Wallet : n'ajoute que des wallets jamais actifs avant ─
            if require_fresh:
                is_fresh = await fund_tracer.is_fresh_wallet(recipient)
                if not is_fresh:
                    log.info(f"⏭️  '{recipient[:8]}...' rejeté (Fresh Wallet actif, wallet déjà actif avant).")
                    continue

            if not self.data_store.has_free_slot():
                log.warning(f"🛑 Limite de {config.MAX_MONITORED_WALLETS} wallets atteinte, '{recipient[:8]}...' ignoré.")
                if self.notifier:
                    await self.notifier.notify(
                        "rugger_alert",
                        f"⚠️ Limite de {config.MAX_MONITORED_WALLETS} wallets monitorés atteinte — "
                        f"nouveau wallet détecté via '{label}' mais pas ajouté.",
                    )
                continue

            self.data_store.add_dev_wallet(
                recipient,
                label=f"Protection auto ({label})",
                scheme=target_type,
                backtest_ratio=0.0,  # hérité, pas backtesté individuellement
                mode="track_creation",
                settings=child_settings,
            )
            # Trace le groupe de liaison pour le Front Run Sell (voir paper_trader.py)
            self.data_store.add_linked_wallet(label, recipient)
            new_count += 1

            if self.notifier:
                await self.notifier.notify(
                    "rugger_alert",
                    f"🛡️ *Rugger Alert*\nNouveau wallet détecté via '{label}'\n"
                    f"Transfert: {r['amount_sol']:.4f} SOL\nAdresse: `{recipient[:8]}...`\nAjouté au monitoring.",
                )

        if new_count:
            log.info(f"🛡️  {new_count} nouveau(x) wallet(s) ajouté(s) via protection '{label}'")

    def _resolve_child_settings(self, target: dict) -> dict:
        parent_address = target.get("parent_address")
        preset_name = target.get("preset_name")

        if parent_address:
            parent_entry = self.data_store.state["monitored_dev_wallets"].get(parent_address)
            if parent_entry:
                parent_settings = parent_entry["settings"]
                if parent_settings.get("inherit_settings", True):
                    return copy.deepcopy(parent_settings)
                else:
                    child_defaults = parent_settings.get("child_defaults", {})
                    merged = copy.deepcopy(config.DEFAULT_WALLET_SETTINGS)
                    merged.update(child_defaults)
                    return merged

        if preset_name:
            return self.data_store.state["presets"].get(preset_name)

        return None
