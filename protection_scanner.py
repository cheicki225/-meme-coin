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
import time

import websockets

import config
import pattern_detector
import fund_tracer
import rpc_client

log = logging.getLogger("protection_scanner")

# Même principe de déduplication que copytrade_listener.py — évite de
# traiter deux fois une notification WebSocket dupliquée pour la même
# transaction (comportement documenté et déjà observé côté Helius).
_SIGNATURE_CACHE_MAX_SIZE = 500
_SIGNATURE_CACHE_TTL_S = 120


class ProtectionScanner:
    def __init__(self, data_store, notifier=None):
        self.data_store = data_store
        self.notifier = notifier
        self._running = False
        self._ws = None
        self._subscribed_addresses = set()
        self._recent_signatures = {}

    async def start(self):
        self._running = True
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
            except Exception as e:
                log.error(f"Connexion WebSocket protection perdue ({e}), reconnexion dans 5s...")
                await asyncio.sleep(5)

    def stop(self):
        self._running = False

    async def _run_connection(self):
        async with websockets.connect(config.HELIUS_WS_URL, ping_interval=20) as ws:
            self._ws = ws
            self._subscribed_addresses = set()

            await self._sync_subscriptions()
            log.info(f"✅ Protection scanner connecté en WebSocket — synchronisation toutes les {config.PROTECTION_SCAN_INTERVAL_S}s.")

            sync_task = asyncio.create_task(self._periodic_sync())
            try:
                async for message in ws:
                    await self._handle_message(message)
            finally:
                sync_task.cancel()

    async def _periodic_sync(self):
        while True:
            await asyncio.sleep(config.PROTECTION_SCAN_INTERVAL_S)
            try:
                await self._sync_subscriptions()
            except Exception as e:
                log.debug(f"Erreur sync protection: {e}")

    async def _sync_subscriptions(self):
        """Ajoute les souscriptions pour les nouvelles cibles de protection —
        même logique que copytrade_listener._sync_subscriptions. Les cibles
        retirées restent souscrites côté RPC jusqu'à la reconnexion suivante
        (pas d'unsubscribe explicite ici) — sans impact fonctionnel."""
        targets = dict(self.data_store.state.get("protection_targets", {}))
        current_addresses = {t["address"] for t in targets.values()}
        new_addresses = current_addresses - self._subscribed_addresses

        for address in new_addresses:
            sub_msg = {
                "jsonrpc": "2.0",
                "id": len(self._subscribed_addresses) + 1,
                "method": "logsSubscribe",
                "params": [{"mentions": [address]}, {"commitment": "confirmed"}],
            }
            await self._ws.send(json.dumps(sub_msg))
            self._subscribed_addresses.add(address)

        if new_addresses:
            log.info(f"🔔 {len(new_addresses)} nouvelle(s) adresse(s) de protection souscrite(s) en WebSocket.")

    async def _handle_message(self, message: str):
        try:
            data = json.loads(message)
            if "result" in data and "id" in data:
                return  # réponse à une souscription, rien à traiter

            result = data.get("params", {}).get("result", {})
            value = result.get("value", {})
            signature = value.get("signature")
            if not signature or self._already_processed(signature):
                return

            await self._process_live_transaction(signature)
        except Exception as e:
            log.debug(f"Erreur traitement message protection: {e}")

    def _already_processed(self, signature: str) -> bool:
        now = time.time()
        if len(self._recent_signatures) > _SIGNATURE_CACHE_MAX_SIZE:
            cutoff = now - _SIGNATURE_CACHE_TTL_S
            self._recent_signatures = {s: t for s, t in self._recent_signatures.items() if t > cutoff}
        if signature in self._recent_signatures:
            return True
        self._recent_signatures[signature] = now
        return False

    async def _process_live_transaction(self, signature: str):
        """Reçoit une notification WebSocket pour une adresse surveillée,
        récupère la transaction parsée, et vérifie si elle contient un
        transfert SOL sortant correspondant aux critères d'une des cibles."""
        parsed = await rpc_client.fetch_parsed_transaction(signature)
        if not parsed:
            return

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
                log.error(f"Erreur scan protection '{label}': {e}")

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
