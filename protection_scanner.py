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

CORRIGÉ suite à une demande explicite (réduction du coût RPC Helius) :
tournait auparavant en sondage RPC complet (find_fixed_amount_recipients/
find_all_outgoing_recipients, pagination jusqu'à 20 pages de 1000
signatures) toutes les PROTECTION_SCAN_INTERVAL_S secondes, pour CHAQUE
cible surveillée — un des postes de coût RPC les plus lourds identifiés,
et qui introduisait en plus un délai structurel contraire au but même de
ce module (minimiser la latence avant "bloc zéro"). Passe maintenant par
un abonnement WebSocket (logsSubscribe, même principe que
copytrade_listener.py) : Helius pousse une notification dès qu'une des
adresses surveillées émet une transaction, au lieu qu'on aille sonder
nous-même en boucle. Un unique scan RPC complet reste fait au démarrage
(_scan_all_targets, méthode batch inchangée) pour rattraper ce qui aurait
pu être manqué pendant que le bot était hors ligne — coût ponctuel, pas
répété.
"""

import asyncio
import copy
import json
import logging

import websockets

import config
import pattern_detector
import fund_tracer
import rpc_client
from execution_metrics import measure_execution
from helius_event_store import (
    EventQueueFullError,
    EventStoreError,
    EventRecord,
    HeliusEventStore,
    RetryableEventError,
    await_subscription_acknowledgements,
    drain_stream,
    get_default_event_store,
    recover_required_streams,
)

log = logging.getLogger("protection_scanner")

class ProtectionScanner:
    def __init__(
        self,
        data_store,
        notifier=None,
        *,
        event_store: HeliusEventStore | None = None,
    ):
        self.data_store = data_store
        self.notifier = notifier
        self._event_store = event_store or get_default_event_store()
        self._running = False
        self._stop_requested = False
        self._ws = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribed_addresses: set[str] = set()
        self._sub_id_to_address: dict[int, str] = {}
        self._request_id_to_address: dict[int, str] = {}
        self._next_request_id = 0

    async def start(self):
        self._running = True
        self._stop_requested = False
        log.info("🛡️  Protection scanner démarré.")

        # Scan RPC complet UNE SEULE FOIS au démarrage — rattrape ce qui a pu
        # être manqué pendant que le bot était hors ligne. Coût ponctuel, pas
        # répété (voir docstring du module pour le raisonnement complet).
        await self._scan_all_targets()

        if not config.HELIUS_WS_URL:
            log.warning("Aucune clé Helius configurée — surveillance protection en direct désactivée (scan initial seul).")
            return

        while self._running:
            try:
                await self._run_connection()
            except EventStoreError:
                self._running = False
                self._stop_requested = True
                log.critical("Protection event storage failed; listener stopped fail-closed")
            except Exception as e:
                log.error("External operation failed (%s)", type(e).__name__)
                await asyncio.sleep(5)

    def stop(self):
        self._running = False
        self._stop_requested = True
        ws = self._ws
        loop = self._loop
        if ws is None or loop is None or not loop.is_running():
            return

        def schedule_close() -> None:
            task = loop.create_task(ws.close())
            task.add_done_callback(
                lambda done: None if done.cancelled() else done.exception()
            )

        try:
            loop.call_soon_threadsafe(schedule_close)
        except RuntimeError:
            return

    async def _run_connection(self):
        self._loop = asyncio.get_running_loop()
        async with websockets.connect(config.HELIUS_WS_URL, ping_interval=20) as ws:
            self._ws = ws
            self._subscribed_addresses = set()
            self._sub_id_to_address = {}
            self._request_id_to_address = {}
            known_streams = set(self._event_store.stream_names("protection:"))

            await self._sync_subscriptions()
            for stream in known_streams:
                self._event_store.mark_recovery_required(stream, "connection_gap")
            buffered_messages = await await_subscription_acknowledgements(
                ws, self._request_id_to_address, self._handle_message
            )
            ready = await recover_required_streams(
                self._event_store,
                "protection:",
                self._process_event_record,
                self._address_for_stream,
                should_stop=lambda: self._stop_requested,
            )
            if not ready:
                log.error("Protection event recovery is incomplete; intake remains paused")
                await ws.close()
                if self._running:
                    await asyncio.sleep(5)
                return
            if not self._running:
                return
            for message in buffered_messages:
                if not self._running:
                    return
                await self._handle_message(message)
            log.info(f"✅ Protection scanner connecté en WebSocket — synchronisation toutes les {config.PROTECTION_SCAN_INTERVAL_S}s.")

            sync_task = asyncio.create_task(self._periodic_sync())
            retry_task = asyncio.create_task(self._retry_pending_events())
            try:
                async for message in ws:
                    await self._handle_message(message)
            finally:
                sync_task.cancel()
                retry_task.cancel()
                for task in (sync_task, retry_task):
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                for stream in self._event_store.stream_names("protection:"):
                    self._event_store.mark_recovery_required(stream, "connection_gap")

    async def _periodic_sync(self):
        while True:
            await asyncio.sleep(config.PROTECTION_SCAN_INTERVAL_S)
            try:
                await self._sync_subscriptions()
            except Exception as e:
                log.debug("External operation failed (%s)", type(e).__name__)

    async def _sync_subscriptions(self):
        """Ajoute les souscriptions pour les nouvelles cibles de protection —
        même logique que copytrade_listener._sync_subscriptions. Les cibles
        retirées restent souscrites côté RPC jusqu'à la reconnexion suivante
        (pas d'unsubscribe explicite ici) — sans impact fonctionnel."""
        targets = dict(self.data_store.state.get("protection_targets", {}))
        current_addresses = {t["address"] for t in targets.values()}
        new_addresses = current_addresses - self._subscribed_addresses

        for address in new_addresses:
            self._next_request_id += 1
            request_id = self._next_request_id
            sub_msg = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "logsSubscribe",
                "params": [{"mentions": [address]}, {"commitment": "confirmed"}],
            }
            await self._ws.send(json.dumps(sub_msg))
            self._event_store.ensure_stream(f"protection:{address}")
            self._request_id_to_address[request_id] = address
            self._subscribed_addresses.add(address)

        if new_addresses:
            log.info(f"🔔 {len(new_addresses)} nouvelle(s) adresse(s) de protection souscrite(s) en WebSocket.")

    async def _handle_message(self, message: str):
        try:
            data = json.loads(message)
            if "error" in data and "id" in data:
                raise ValueError("Protection subscription was rejected")
            if "result" in data and "id" in data:
                request_id = data.get("id")
                subscription_id = data.get("result")
                address = self._request_id_to_address.pop(request_id, None)
                if not address or type(subscription_id) is not int:
                    raise ValueError("Protection subscription acknowledgement is invalid")
                self._sub_id_to_address[subscription_id] = address
                return

            params = data.get("params", {})
            result = params.get("result", {})
            value = result.get("value", {})
            signature = value.get("signature")
            if not isinstance(signature, str) or not signature:
                if "params" in data:
                    raise ValueError("Protection notification has no valid signature")
                return

            subscription_id = params.get("subscription")
            address = self._sub_id_to_address.get(subscription_id)
            if address is None and len(self._subscribed_addresses) == 1:
                address = next(iter(self._subscribed_addresses))
            if address is None:
                for tracked_address in self._subscribed_addresses:
                    self._event_store.mark_recovery_required(
                        f"protection:{tracked_address}", "subscription_mapping"
                    )
                log.error("Protection subscription mapping is unavailable; intake paused")
                if self._ws is not None:
                    await self._ws.close()
                return

            context = result.get("context", {})
            slot = context.get("slot") if isinstance(context, dict) else None
            await self._process_live_transaction(
                signature,
                source=f"protection:{address}",
                slot=slot if type(slot) is int else None,
            )
        except EventQueueFullError:
            log.error("Protection event inbox full; pausing WebSocket intake for history replay")
            if self._ws is not None:
                await self._ws.close()
        except EventStoreError:
            self._running = False
            self._stop_requested = True
            log.critical("Protection event storage failed; listener stopped fail-closed")
            if self._ws is not None:
                await self._ws.close()
            raise
        except Exception as e:
            for address in self._subscribed_addresses:
                self._event_store.mark_recovery_required(
                    f"protection:{address}", "invalid_notification"
                )
            log.error("Protection notification rejected (%s); reconnecting for replay", type(e).__name__)
            if self._ws is not None:
                await self._ws.close()

    @staticmethod
    def _address_for_stream(stream: str) -> str | None:
        address = stream.removeprefix("protection:")
        return address if address else None

    async def _process_live_transaction(
        self,
        signature: str,
        *,
        source: str | None = None,
        slot: int | None = None,
        transaction_index: int | None = None,
        is_replay: bool = False,
    ):
        """Persist first, then parse and evaluate one protection event."""
        if source is None:
            if len(self._subscribed_addresses) == 1:
                source = f"protection:{next(iter(self._subscribed_addresses))}"
            else:
                source = "protection:unmapped"
        self._event_store.enqueue(
            source,
            signature,
            {"replayed": bool(is_replay)},
            slot=slot,
            transaction_index=transaction_index,
            is_replay=is_replay,
        )

        result = await self._drain_event_stream(source)
        if result == "unknown" and self._ws is not None:
            await self._ws.close()

    async def _drain_event_stream(self, stream: str) -> str:
        result = await drain_stream(
            self._event_store,
            stream,
            self._process_event_record,
            should_stop=lambda: self._stop_requested,
        )
        if result == "unknown":
            log.error("Protection event stream is blocked pending manual review")
        return result

    async def _retry_pending_events(self):
        while True:
            try:
                for stream in self._event_store.stream_names("protection:"):
                    result = await self._drain_event_stream(stream)
                    if result == "unknown" and self._ws is not None:
                        await self._ws.close()
                await asyncio.sleep(1)
            except EventStoreError:
                self._running = False
                self._stop_requested = True
                log.critical("Protection event storage failed; listener stopped fail-closed")
                if self._ws is not None:
                    await self._ws.close()
                return

    async def _process_event_record(self, event: EventRecord) -> None:
        with measure_execution("event.parse", "helius"):
            parsed = await rpc_client.fetch_parsed_transaction(event.signature)
        if not parsed:
            raise RetryableEventError("enhanced_fetch_unavailable")

        self._event_store.mark_effect_started(
            event.event_id,
            claim_generation=event.claim_generation,
            recovery=event.recovery_claim,
            recovery_generation=event.recovery_generation,
        )
        with measure_execution("event.decision", "internal"):
            await self._process_parsed_transaction(parsed)

    async def _process_parsed_transaction(self, parsed: dict) -> None:
        native_transfers = parsed.get("nativeTransfers", []) or []
        if not native_transfers:
            return

        targets = dict(self.data_store.state.get("protection_targets", {}))
        for label, target in targets.items():
            address = target["address"]
            for nt in native_transfers:
                if nt.get("fromUserAccount") != address:
                    continue
                recipient = nt.get("toUserAccount")
                amount_lamports = nt.get("amount", 0)
                if not recipient or not amount_lamports:
                    continue
                amount_sol = amount_lamports / 1_000_000_000

                # AJOUTÉ (demande explicite, 19 août) : un intervalle défini
                # DIRECTEMENT sur la cible (ajout manuel en une étape) est
                # prioritaire sur celui hérité d'un wallet parent.
                parent_ranges = target.get("ranges")
                if not parent_ranges:
                    child_settings = self._resolve_child_settings(target)
                    parent_ranges = child_settings.get("transfer_ranges") if child_settings else None
                else:
                    child_settings = self._resolve_child_settings(target)

                if not self._matches_target_criteria(target, amount_sol, parent_ranges):
                    continue

                require_fresh = child_settings.get("fresh_wallet_only", False) if child_settings else False
                await self._process_recipient(label, target["type"], recipient, amount_sol, child_settings, require_fresh)

    def _matches_target_criteria(self, target: dict, amount_sol: float, parent_ranges) -> bool:
        """Réplique la logique de filtrage par montant qu'appliquaient
        find_fixed_amount_recipients/find_multi_range_recipients (ancien scan
        RPC batch) — 'mere' accepte n'importe quel montant, 'exchange' filtre
        par plage(s) ou montant fixe ± tolérance."""
        if target["type"] == "mere":
            return True
        if target["type"] != "exchange":
            return False

        if parent_ranges:
            return any(r["min"] <= amount_sol <= r["max"] for r in parent_ranges)

        target_amount = target.get("amount_sol")
        tolerance = target.get("tolerance") or 0
        if target_amount is None:
            return False
        return abs(amount_sol - target_amount) <= tolerance

    async def _scan_all_targets(self):
        targets = dict(self.data_store.state.get("protection_targets", {}))
        if not targets:
            return

        for label, target in targets.items():
            try:
                await self._scan_one_target(label, target)
            except Exception as e:
                log.error("External operation failed (%s)", type(e).__name__)

    async def _scan_one_target(self, label: str, target: dict):
        """Scan RPC batch — utilisé UNE SEULE FOIS au démarrage (voir start())
        pour rattraper ce qui a pu être manqué hors ligne. La surveillance
        continue passe par le WebSocket (_process_live_transaction)."""
        target_type = target["type"]
        address = target["address"]

        # Résout les settings du parent EN AMONT — nécessaire pour connaître
        # transfer_ranges (plusieurs plages) et fresh_wallet_only avant de
        # choisir la méthode de scan et de valider chaque nouveau wallet.
        # AJOUTÉ (demande explicite, 19 août) : un intervalle défini
        # DIRECTEMENT sur la cible (ajout manuel en une étape) est
        # prioritaire sur celui hérité d'un wallet parent.
        child_settings = self._resolve_child_settings(target)
        parent_ranges = target.get("ranges") or (child_settings.get("transfer_ranges") if child_settings else None)

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

        for r in recipients:
            await self._process_recipient(label, target_type, r["recipient"], r["amount_sol"], child_settings, require_fresh)

    async def _process_recipient(self, label: str, target_type: str, recipient: str, amount_sol: float,
                                  child_settings: dict, require_fresh: bool):
        """Traite UN destinataire candidat (déjà filtré par montant/plage en
        amont) — dédup, vérification Fresh Wallet, ajout au monitoring,
        notification. EXTRAIT de l'ancien _scan_one_target pour être partagé
        entre le scan batch initial et la détection en direct WebSocket."""
        if self.data_store.is_recipient_known(label, recipient):
            return
        self.data_store.mark_recipient_known(label, recipient)

        if self.data_store.is_dev_monitored(recipient):
            return

        # ── Fresh Wallet : n'ajoute que des wallets jamais actifs avant ─
        if require_fresh:
            is_fresh = await fund_tracer.is_fresh_wallet(recipient)
            if not is_fresh:
                log.info(f"⏭️  '{recipient[:8]}...' rejeté (Fresh Wallet actif, wallet déjà actif avant).")
                return

        if not self.data_store.has_free_slot():
            log.warning(f"🛑 Limite de {config.MAX_MONITORED_WALLETS} wallets atteinte, '{recipient[:8]}...' ignoré.")
            if self.notifier:
                await self.notifier.notify(
                    "rugger_alert",
                    f"⚠️ Limite de {config.MAX_MONITORED_WALLETS} wallets monitorés atteinte — "
                    f"nouveau wallet détecté via '{label}' mais pas ajouté.",
                )
            return

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
        log.info(f"🛡️  Nouveau wallet ajouté via protection '{label}' : {recipient[:8]}...")

        if self.notifier:
            await self.notifier.notify(
                "rugger_alert",
                f"🛡️ *Rugger Alert*\nNouveau wallet détecté via '{label}'\n"
                f"Transfert: {amount_sol:.4f} SOL\nAdresse: `{recipient[:8]}...`\nAjouté au monitoring.",
            )

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
