"""
════════════════════════════════════════════════════════════════
POSITION PRICE STREAM — Surveillance de position en WebSocket (accountSubscribe)
════════════════════════════════════════════════════════════════
AJOUTÉ suite à une demande explicite : remplace le sondage RPC répété
(getAccountInfo toutes les 5-15s par position ouverte, dans
paper_trader._monitor_position) par un abonnement WebSocket accountSubscribe
sur le compte bonding curve de chaque position — Helius POUSSE une mise à
jour à chaque trade sur ce token au lieu qu'on aille la chercher en boucle.

Pourquoi : le sondage RPC répété était l'un des plus gros postes de coût
Helius identifiés (jusqu'à 240-720 appels RPC par position sur sa durée de
vie). Un accountSubscribe est facturé au volume de données transféré
(~2 crédits/0.1 Mo chez Helius), pas au nombre de mises à jour — pour un
compte bonding curve (quelques dizaines d'octets par mise à jour), c'est
très largement moins cher qu'un sondage répété.

Une seule connexion WebSocket pour TOUTES les positions ouvertes
simultanément — accountSubscribe supporte plusieurs souscriptions sur une
même connexion (confirmé par la documentation Helius).

Filet de sécurité : ce module ne remplace PAS totalement le sondage RPC —
paper_trader._monitor_position garde un sondage de repli à intervalle bien
plus long (voir SAFETY_POLL_TIMEOUT_S ci-dessous) si aucune mise à jour
WebSocket n'arrive dans ce délai, pour rester protégé contre une
déconnexion silencieuse, un message manqué, ou le temps de reconnexion
après une coupure réseau.
"""

import asyncio
import base64
import json
import logging

import websockets

import config
import backtest

log = logging.getLogger("position_stream")

# Si aucune mise à jour WebSocket n'arrive dans ce délai pour une position
# suivie, paper_trader._monitor_position doit basculer sur un sondage RPC
# de repli — voir le docstring du module.
SAFETY_POLL_TIMEOUT_S = 60


class PositionPriceStream:
    def __init__(self):
        self._ws = None
        self._running = False
        self._connected_event = asyncio.Event()

        self._mint_to_bonding_curve = {}   # token_mint -> bonding_curve_address
        self._mint_to_sub_id = {}          # token_mint -> subscription id renvoyé par Helius
        self._sub_id_to_mint = {}          # sens inverse, pour router les notifications reçues
        self._queues = {}                  # token_mint -> asyncio.Queue des mises à jour poussées
        self._pending_subscriptions = {}   # request id (notre id sortant) -> token_mint, en attendant la confirmation

    async def start(self):
        if not config.HELIUS_WS_URL:
            log.warning("Aucune clé Helius configurée — surveillance de position WebSocket ne démarre pas.")
            return
        self._running = True
        while self._running:
            try:
                await self._run_connection()
            except Exception as e:
                log.error(f"Connexion WebSocket surveillance de position perdue ({e}), reconnexion dans 5s...")
                self._connected_event.clear()
                await asyncio.sleep(5)

    def stop(self):
        self._running = False

    async def _run_connection(self):
        async with websockets.connect(config.HELIUS_WS_URL, ping_interval=20) as ws:
            self._ws = ws
            # Une reconnexion invalide tous les sub_id précédents (ils sont
            # propres à l'ancienne connexion) — re-souscrit tout ce qui était
            # encore suivi au moment de la coupure.
            self._mint_to_sub_id = {}
            self._sub_id_to_mint = {}
            for token_mint, bonding_curve_address in list(self._mint_to_bonding_curve.items()):
                await self._send_subscribe(token_mint, bonding_curve_address)

            self._connected_event.set()
            log.info(f"✅ Surveillance de position WebSocket connectée ({len(self._mint_to_bonding_curve)} position(s) re-souscrite(s)).")

            try:
                async for message in ws:
                    await self._handle_message(message)
            finally:
                self._connected_event.clear()

    async def _send_subscribe(self, token_mint: str, bonding_curve_address: str):
        req_id = abs(hash(bonding_curve_address)) % 1_000_000_000
        sub_msg = {
            "jsonrpc": "2.0", "id": req_id, "method": "accountSubscribe",
            "params": [bonding_curve_address, {"encoding": "base64", "commitment": "confirmed"}],
        }
        self._pending_subscriptions[req_id] = token_mint
        await self._ws.send(json.dumps(sub_msg))

    async def subscribe(self, token_mint: str) -> asyncio.Queue:
        """
        Démarre le suivi d'un token — retourne une Queue sur laquelle
        paper_trader._monitor_position peut attendre les mises à jour
        {"price_sol", "market_cap_usd", "complete"} poussées par Helius dès
        qu'un trade change le compte bonding curve. Idempotent : rappeler
        avec le même token_mint retourne la même queue existante.
        """
        if token_mint in self._queues:
            return self._queues[token_mint]

        try:
            bonding_curve_address = backtest._get_bonding_curve_address(token_mint)
        except Exception as e:
            log.warning(f"⚠️ Surveillance WS — dérivation PDA impossible pour {token_mint[:8]}...: {e}")
            queue = asyncio.Queue(maxsize=50)
            self._queues[token_mint] = queue
            return queue

        queue = asyncio.Queue(maxsize=50)
        self._queues[token_mint] = queue
        self._mint_to_bonding_curve[token_mint] = bonding_curve_address

        # Attend que la connexion soit établie avant de souscrire — au
        # démarrage du bot, la surveillance d'une position peut commencer
        # avant que la connexion WS ne soit prête. Timeout raisonnable ; le
        # sondage de repli (paper_trader) prend le relais si ça traîne.
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=10)
            await self._send_subscribe(token_mint, bonding_curve_address)
        except asyncio.TimeoutError:
            log.warning(f"⏱️  Surveillance WS pas encore connectée pour {token_mint[:8]}..., le sondage de repli prendra le relais en attendant.")

        return queue

    async def unsubscribe(self, token_mint: str):
        """Arrête le suivi d'un token — à appeler dès qu'une position se ferme
        (toute raison de fermeture confondue), sans quoi les souscriptions
        s'accumulent indéfiniment sur la durée de vie du bot."""
        self._mint_to_bonding_curve.pop(token_mint, None)
        self._queues.pop(token_mint, None)
        sub_id = self._mint_to_sub_id.pop(token_mint, None)
        if sub_id is not None:
            self._sub_id_to_mint.pop(sub_id, None)
            if self._ws:
                try:
                    await self._ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": 1, "method": "accountUnsubscribe", "params": [sub_id],
                    }))
                except Exception:
                    pass  # connexion probablement déjà fermée — sans conséquence, la souscription meurt avec elle

    async def _handle_message(self, message: str):
        try:
            data = json.loads(message)

            # Réponse à une demande de souscription (contient notre id
            # sortant, pas de "params") — associe le sub_id renvoyé par
            # Helius au token concerné pour router les notifications futures
            # et pouvoir se désabonner proprement plus tard.
            if "result" in data and "id" in data:
                req_id = data["id"]
                token_mint = self._pending_subscriptions.pop(req_id, None)
                if token_mint:
                    sub_id = data["result"]
                    self._mint_to_sub_id[token_mint] = sub_id
                    self._sub_id_to_mint[sub_id] = token_mint
                return

            result = data.get("params", {}).get("result", {})
            value = result.get("value", {})
            raw_data = value.get("data")
            if not raw_data:
                return

            sub_id = data.get("params", {}).get("subscription")
            token_mint = self._sub_id_to_mint.get(sub_id)
            if not token_mint:
                return

            raw = base64.b64decode(raw_data[0])
            decoded = backtest._decode_bonding_curve_bytes(raw)
            if not decoded:
                return

            price_info = await backtest._finalize_bonding_curve_price(decoded)
            queue = self._queues.get(token_mint)
            if queue:
                if queue.full():
                    try:
                        queue.get_nowait()  # évite un blocage si le consommateur est en retard — garde la mise à jour la plus récente
                    except asyncio.QueueEmpty:
                        pass
                queue.put_nowait(price_info)
        except Exception as e:
            log.debug(f"Erreur traitement message surveillance position: {e}")
