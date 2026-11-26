"""
════════════════════════════════════════════════════════════════
DÉTECTION TEMPS RÉEL — Nouveaux tokens Pump.fun
════════════════════════════════════════════════════════════════
Écoute en continu le programme Pump.fun via WebSocket (logsSubscribe)
et déclenche un callback dès qu'une création de token est détectée.

Fonctionnement :
1. On s'abonne aux logs qui mentionnent le programme Pump.fun
2. Dès qu'une transaction arrive, on récupère sa version "parsée"
   via l'API Enhanced Transactions de Helius (qui reconnaît déjà
   les instructions Pump.fun : CREATE / BUY / SELL)
3. Si c'est une création de token, on appelle le callback fourni
   avec les infos utiles (adresse du token, adresse du dev, signature)

Prérequis : une clé Helius valide dans config.py (HELIUS_API_KEY).
Sans clé, on retombe sur le RPC public — fonctionnera mais avec
une latence et des limites de débit qui rendent le sniping peu fiable.
"""

import asyncio
import json
import logging
import random
import time
import aiohttp
import websockets

import config
import rpc_client
from execution_metrics import measure_execution
from helius_event_store import (
    CanonicalOrderRequiredError,
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
import wallet

log = logging.getLogger("detection")

_RECONNECT_MAX_DELAY_S = 30.0
_RECONNECT_STABLE_CONNECTION_S = 60.0


class NewTokenListener:
    """
    Écoute les créations de tokens Pump.fun en temps réel et notifie
    un callback asynchrone pour chaque nouveau token détecté.

    MODIFIÉ (demande explicite, 19 août — réduction de coût) : deux modes
    de souscription selon state["auto_detection_settings"]["auto_add_new_devs"] :
    - True (découverte de nouveaux devs active) : écoute TOUT le programme
      Pump.fun (comportement d'origine) — nécessaire pour voir des devs
      encore inconnus.
    - False (découverte désactivée, cas de Cheicki actuellement) : écoute
      UNIQUEMENT les wallets déjà dans monitored_dev_wallets (mode
      track_creation), une souscription par wallet — même principe que
      copytrade_listener.py (qui surveille déjà individuellement ces mêmes
      wallets pour l'alerte transfert 90%). Réduit drastiquement le volume
      d'appels à l'API Enhanced Transactions (le facteur de coût dominant,
      confirmé par Cheicki via le tableau de coûts Helius) puisqu'on ne
      traite plus que les transactions de TES wallets, pas toute la
      plateforme.

    Ce choix se fait à CHAQUE reconnexion (pas en direct pendant qu'une
    connexion est active) — si tu changes ce réglage en cours de route, ça
    prend effet à la prochaine reconnexion (généralement dans les
    5-90s vu la fréquence des reconnexions observées ce soir), pas besoin
    de redémarrer le bot.
    """

    def __init__(
        self,
        on_new_token,
        data_store=None,
        *,
        event_store: HeliusEventStore | None = None,
    ):
        """
        on_new_token: fonction async(dict) appelée pour chaque nouveau token.
            Le dict contient : {
                "token_mint": str,
                "dev_address": str,
                "signature": str,
                "raw": dict  # transaction parsée complète (Helius)
            }
        data_store: AJOUTÉ (demande explicite) — instance de
            monitoring_list.DataStore, nécessaire pour le mode économique
            par wallet. None = toujours en mode "toute la plateforme"
            (comportement d'origine), même si auto_add_new_devs est False.
        """
        self.on_new_token = on_new_token
        self._event_store = event_store or get_default_event_store()
        self.data_store = data_store
        self._running = False
        self._stop_requested = False
        self._ws = None
        self._stop_event: asyncio.Event | None = None
        self._subscribed_wallets: set[str] = set()
        self._per_wallet_mode = False
        self._sub_id_to_stream: dict[int, str] = {}
        self._request_id_to_stream: dict[int, str] = {}
        self._next_request_id = 0

    async def start(self):
        if not config.HELIUS_WS_URL:
            log.warning(
                "Aucune clé Helius configurée — le WebSocket ne peut pas démarrer. "
                "Ajoute HELIUS_API_KEY dans tes variables d'environnement."
            )
            return

        self._running = True
        self._stop_requested = False
        self._stop_event = asyncio.Event()
        reconnect_attempt = 0
        while self._running:
            started = time.monotonic()
            try:
                await self._listen_once()
                if self._running:
                    log.warning("WebSocket stream closed; scheduling a reconnect")
            except asyncio.CancelledError:
                raise
            except EventStoreError:
                self._running = False
                self._stop_requested = True
                log.critical("New-token event storage failed; listener stopped fail-closed")
            except Exception as e:
                log.error("External operation failed (%s)", type(e).__name__)
            finally:
                self._ws = None

            if not self._running:
                break
            if time.monotonic() - started >= _RECONNECT_STABLE_CONNECTION_S:
                reconnect_attempt = 0
            delay = self._reconnect_delay(reconnect_attempt)
            reconnect_attempt = min(reconnect_attempt + 1, 16)
            log.info("WebSocket reconnect scheduled in %.2f seconds", delay)
            with measure_execution("websocket.reconnect_wait", "solana_ws") as metric:
                completed = await self._wait_before_reconnect(delay)
                metric["outcome"] = "success" if completed and self._running else "skipped"

    async def stop(self):
        self._running = False
        self._stop_requested = True
        if self._stop_event is not None:
            self._stop_event.set()
        ws = self._ws
        if ws is not None:
            await ws.close()

    @staticmethod
    def _reconnect_delay(attempt: int) -> float:
        """Return bounded exponential backoff with positive jitter."""
        base = min(0.5 * (2 ** max(attempt, 0)), 24.0)
        jitter = random.uniform(0.0, min(base * 0.25, 6.0))
        return min(base + jitter, _RECONNECT_MAX_DELAY_S)

    async def _wait_before_reconnect(self, delay: float) -> bool:
        """Wait for the backoff delay; return false when stop interrupts it."""
        if self._stop_event is None:
            await asyncio.sleep(delay)
            return True
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            return True
        return False

    def _should_use_per_wallet_mode(self) -> bool:
        if not self.data_store:
            return False
        det_settings = self.data_store.state.get("auto_detection_settings", {})
        return not det_settings.get("auto_add_new_devs", True)

    def _get_tracked_creation_wallets(self) -> set:
        wallets = set()
        for address, entry in self.data_store.state.get("monitored_dev_wallets", {}).items():
            if entry.get("mode", "track_creation") == "track_creation":
                wallets.add(address)
        return wallets

    async def _listen_once(self):
        self._per_wallet_mode = self._should_use_per_wallet_mode()

        async with websockets.connect(config.HELIUS_WS_URL, ping_interval=20) as ws:
            self._ws = ws
            self._sub_id_to_stream = {}
            self._request_id_to_stream = {}
            known_streams = set(self._event_store.stream_names("new-token:"))
            sync_task = None
            retry_task = None
            if not self._running:
                return

            if self._per_wallet_mode:
                self._subscribed_wallets = set()
                await self._sync_wallet_subscriptions()
                log.info(
                    f"✅ Abonné aux logs Pump.fun — mode économique, "
                    f"{len(self._subscribed_wallets)} wallet(s) suivi(s) seulement "
                    f"(découverte de nouveaux devs désactivée)."
                )
                sync_task = asyncio.create_task(self._periodic_sync())
            else:
                subscribe_msg = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [config.PUMP_FUN_PROGRAM_ID]},
                        {"commitment": "processed"},
                    ],
                }
                with measure_execution("websocket.subscription_send", "solana_ws"):
                    await ws.send(json.dumps(subscribe_msg))
                self._request_id_to_stream[1] = (
                    f"new-token:{config.PUMP_FUN_PROGRAM_ID}"
                )
                self._event_store.ensure_stream(
                    f"new-token:{config.PUMP_FUN_PROGRAM_ID}"
                )
                log.info("✅ Abonné aux logs Pump.fun — écoute en cours (toute la plateforme)...")

            try:
                for stream in known_streams:
                    self._event_store.mark_recovery_required(stream, "connection_gap")
                buffered_messages = await await_subscription_acknowledgements(
                    ws, self._request_id_to_stream, self._handle_notification
                )
                ready = await recover_required_streams(
                    self._event_store,
                    "new-token:",
                    self._process_event_record,
                    self._address_for_stream,
                    should_stop=lambda: self._stop_requested,
                )
                if not ready:
                    log.error("New-token event recovery is incomplete; intake remains paused")
                    await ws.close()
                    return
                if not self._running:
                    return
                for message in buffered_messages:
                    if not self._running:
                        return
                    await self._handle_notification(message)
                retry_task = asyncio.create_task(self._retry_pending_events())
                async for message in ws:
                    await self._handle_notification(message)
            finally:
                if sync_task:
                    sync_task.cancel()
                    try:
                        await sync_task
                    except asyncio.CancelledError:
                        pass
                if retry_task:
                    retry_task.cancel()
                    try:
                        await retry_task
                    except asyncio.CancelledError:
                        pass
                for stream in self._event_store.stream_names("new-token:"):
                    self._event_store.mark_recovery_required(stream, "connection_gap")
                self._ws = None

    def _active_streams(self) -> set[str]:
        if self._per_wallet_mode:
            return {f"new-token:{address}" for address in self._subscribed_wallets}
        return {f"new-token:{config.PUMP_FUN_PROGRAM_ID}"}

    @staticmethod
    def _address_for_stream(stream: str) -> str | None:
        address = stream.removeprefix("new-token:")
        return address if address else None

    async def _handle_notification(self, message: str) -> None:
        try:
            data = json.loads(message)
            if "error" in data and "id" in data:
                raise ValueError("New-token subscription was rejected")
            if "result" in data and "id" in data:
                request_id = data.get("id")
                subscription_id = data.get("result")
                stream = self._request_id_to_stream.pop(request_id, None)
                if not stream or type(subscription_id) is not int:
                    raise ValueError("New-token subscription acknowledgement is invalid")
                self._sub_id_to_stream[subscription_id] = stream
                return

            params = data.get("params", {})
            result = params.get("result", {})
            value = result.get("value", {})
            signature = value.get("signature")
            logs = value.get("logs", [])
            if not isinstance(signature, str) or not signature:
                if "params" in data:
                    raise ValueError("New-token notification has no valid signature")
                return
            if not isinstance(logs, list):
                raise ValueError("New-token notification has invalid logs")
            if not any(
                isinstance(line, str) and "Instruction: Create" in line
                for line in logs
            ):
                return

            subscription_id = params.get("subscription")
            stream = self._sub_id_to_stream.get(subscription_id)
            if stream is None and len(self._active_streams()) == 1:
                stream = next(iter(self._active_streams()))
            if stream is None:
                for active_stream in self._active_streams():
                    self._event_store.mark_recovery_required(
                        active_stream, "subscription_mapping"
                    )
                log.error("New-token subscription mapping is unavailable; intake paused")
                if self._ws is not None:
                    await self._ws.close()
                return

            context = result.get("context", {})
            slot = context.get("slot") if isinstance(context, dict) else None
            await self._handle_creation(
                signature, stream=stream, slot=slot if type(slot) is int else None
            )
        except EventQueueFullError:
            log.error("New-token event inbox full; pausing WebSocket intake for history replay")
            if self._ws is not None:
                await self._ws.close()
        except EventStoreError:
            self._running = False
            self._stop_requested = True
            log.critical("New-token event storage failed; listener stopped fail-closed")
            if self._ws is not None:
                await self._ws.close()
        except Exception as exc:
            for stream in self._active_streams():
                self._event_store.mark_recovery_required(
                    stream, "invalid_notification"
                )
            log.error("New-token notification rejected (%s); reconnecting for replay", type(exc).__name__)
            if self._ws is not None:
                await self._ws.close()

    async def _periodic_sync(self):
        while True:
            await asyncio.sleep(config.COPYTRADE_SYNC_INTERVAL_S)
            try:
                await self._sync_wallet_subscriptions()
            except Exception as e:
                log.debug("External operation failed (%s)", type(e).__name__)

    async def _sync_wallet_subscriptions(self):
        current = self._get_tracked_creation_wallets()
        new_wallets = current - self._subscribed_wallets

        for wallet_addr in new_wallets:
            self._next_request_id += 1
            request_id = self._next_request_id
            stream = f"new-token:{wallet_addr}"
            sub_msg = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "logsSubscribe",
                "params": [{"mentions": [wallet_addr]}, {"commitment": "processed"}],
            }
            with measure_execution("websocket.subscription_send", "solana_ws"):
                await self._ws.send(json.dumps(sub_msg))
            self._event_store.ensure_stream(stream)
            self._request_id_to_stream[request_id] = stream
            self._subscribed_wallets.add(wallet_addr)

        if new_wallets:
            log.info(f"🔔 {len(new_wallets)} wallet(s) de plus souscrit(s) en détection (mode économique).")

    async def _handle_creation(
        self,
        signature: str,
        *,
        stream: str | None = None,
        slot: int | None = None,
        transaction_index: int | None = None,
        is_replay: bool = False,
    ):
        """Persist a detected creation event before fetching its Enhanced transaction."""
        stream = stream or f"new-token:{config.PUMP_FUN_PROGRAM_ID}"
        try:
            self._event_store.enqueue(
                stream,
                signature,
                {"replayed": bool(is_replay)},
                slot=slot,
                transaction_index=transaction_index,
                is_replay=is_replay,
            )
        except EventQueueFullError:
            log.error("New-token event inbox full; pausing WebSocket intake for history replay")
            if self._ws is not None:
                await self._ws.close()
            return

        result = await drain_stream(
            self._event_store,
            stream,
            self._process_event_record,
            should_stop=lambda: self._stop_requested,
        )
        if result == "unknown" and self._ws is not None:
            await self._ws.close()

    async def _retry_pending_events(self):
        while True:
            try:
                for stream in self._event_store.stream_names("new-token:"):
                    result = await drain_stream(
                        self._event_store,
                        stream,
                        self._process_event_record,
                        should_stop=lambda: self._stop_requested,
                    )
                    if result == "unknown" and self._ws is not None:
                        await self._ws.close()
                await asyncio.sleep(1)
            except EventStoreError:
                self._running = False
                self._stop_requested = True
                log.critical("New-token event storage failed; listener stopped fail-closed")
                if self._ws is not None:
                    await self._ws.close()
                return

    async def _process_event_record(self, event: EventRecord) -> None:
        if event.slot is None or event.transaction_index is None:
            raise CanonicalOrderRequiredError("canonical_order_missing")
        with measure_execution("event.parse", "helius"):
            parsed = await self._fetch_parsed_transaction(event.signature)
        if not parsed:
            raise RetryableEventError("enhanced_fetch_unavailable")

        token_mint, dev_address = self._extract_creation_info(parsed)
        if not token_mint or not dev_address:
            return

        self._event_store.mark_effect_started(
            event.event_id,
            claim_generation=event.claim_generation,
            recovery=event.recovery_claim,
            recovery_generation=event.recovery_generation,
        )
        with measure_execution("event.decision", "internal"):
            await self.on_new_token({
                "token_mint": token_mint,
                "dev_address": dev_address,
                "signature": event.signature,
                "creation_slot": parsed.get("slot"),
                "raw": parsed,
            })

    async def _fetch_parsed_transaction(self, signature: str) -> dict:
        """
        Utilise l'API Enhanced Transactions de Helius pour avoir une version lisible.

        CORRIGÉ : utilisait sa propre session aiohttp isolée, sans le fix DNS
        (ThreadedResolver) ni le limiteur de débit global — les deux vivent
        dans rpc_client.py et doivent être appliqués partout où on parle à
        Helius, y compris ici dans la boucle de détection temps réel.
        """
        with measure_execution("websocket.parse_transaction", "helius") as metric:
            if not config.HELIUS_API_KEY:
                metric["outcome"] = "skipped"
                return {}
            try:
                with measure_execution("rpc.rate_limit_wait", "helius") as limit_metric:
                    if not await rpc_client.wait_for_provider_slot(config.HELIUS_PARSE_TX_URL):
                        limit_metric["outcome"] = "skipped"
                        metric["outcome"] = "failure"
                        return {}
                async with aiohttp.ClientSession(connector=rpc_client.get_http_connector(), connector_owner=False) as session:
                    async with session.post(
                        config.HELIUS_PARSE_TX_URL,
                        json={"transactions": [signature]},
                        timeout=aiohttp.ClientTimeout(total=10),
                        allow_redirects=False,
                    ) as resp:
                        if resp.status == 429:
                            rpc_client.note_rate_limited_response(
                                config.HELIUS_PARSE_TX_URL,
                                resp.headers.get("Retry-After"),
                            )
                            log.debug("Helius parse returned HTTP 429")
                            metric["outcome"] = "failure"
                            return {}
                        if resp.status != 200:
                            log.debug("Helius parse returned HTTP %s", resp.status)
                            metric["outcome"] = "failure"
                            return {}
                        data = await resp.json()
                        parsed = data[0] if isinstance(data, list) and data else {}
                        if not parsed:
                            metric["outcome"] = "failure"
                        return parsed
            except Exception as e:
                log.debug("External operation failed (%s)", type(e).__name__)
                metric["outcome"] = "failure"
                return {}

    def _extract_creation_info(self, parsed: dict) -> tuple:
        """
        Extrait (token_mint, dev_address) d'une transaction parsée Helius.
        Helius expose généralement 'tokenTransfers' et 'events.compressed' ou
        des champs custom pour Pump.fun selon leur version d'API — on tente
        plusieurs chemins pour rester robuste aux évolutions du format.
        """
        # Chemin 1 : événement custom Pump.fun (si présent)
        events = parsed.get("events", {})
        pump_event = events.get("pump_fun") or events.get("pumpFun")
        if pump_event:
            return pump_event.get("mint"), pump_event.get("creator") or parsed.get("feePayer")

        # Chemin 2 : fallback générique — le fee payer est presque toujours le dev
        #
        # CORRIGÉ suite à un vrai bug trouvé (tokens "So111111..." et
        # "EPjFWdd5..." vus détectés comme "nouveaux tokens" en conditions
        # réelles — ce sont respectivement le mint SOL natif et le mint
        # USDC, jamais de vrais nouveaux tokens Pump.fun) : ce chemin de
        # repli prenait tokenTransfers[0] sans vérifier lequel des transferts
        # correspond réellement au nouveau token créé — si la transaction
        # groupe création+achat, un mouvement SOL/USDC intermédiaire peut
        # apparaître avant le vrai nouveau mint dans la liste. Exclut
        # maintenant les mints de quote connus (SOL, USDC, USDT) plutôt que
        # de prendre le premier transfert sans distinction.
        dev_address = parsed.get("feePayer")
        token_transfers = parsed.get("tokenTransfers", [])
        excluded_mints = {config.SOL_MINT, wallet.USDC_MINT, "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}  # USDT
        token_mint = next(
            (tt.get("mint") for tt in token_transfers if tt.get("mint") not in excluded_mints),
            None,
        )

        return token_mint, dev_address


# ── Test manuel autonome ──────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def on_token(info):
        print(f"🆕 Nouveau token détecté : {info['token_mint']} (dev: {info['dev_address']})")

    listener = NewTokenListener(on_new_token=on_token)
    asyncio.run(listener.start())
