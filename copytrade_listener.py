"""
════════════════════════════════════════════════════════════════
COPYTRADE LISTENER — Détection temps réel achats/ventes (track_buy/track_sell)
════════════════════════════════════════════════════════════════
Contrairement à websocket_listener.py (qui écoute le programme Pump.fun
pour repérer des CRÉATIONS de tokens), ce module écoute directement les
wallets ajoutés en mode "track_buy" ou "track_sell" pour copier leurs
achats/ventes — méthode 2 des vidéos ("copy trade de rugger").

Fonctionnement :
1. Une souscription WebSocket par wallet suivi (logsSubscribe avec
   mentions:[wallet]) — les wallets sont ajoutés/retirés dynamiquement
   en fonction de monitored_dev_wallets (poll toutes les 30s)
2. Dès qu'une transaction du wallet arrive, on récupère sa version parsée
   (API Enhanced Transactions Helius) et on détermine si c'est un ACHAT
   ou une VENTE via les tokenTransfers/nativeTransfers
3. On déclenche le callback approprié (on_buy / on_sell)

LIMITE CONNUE : la classification achat/vente est une heuristique basée
sur le sens des transferts (le wallet reçoit un token + envoie du SOL =
achat ; l'inverse = vente). Ça couvre le cas standard d'un swap Pump.fun/
Raydium mais peut se tromper sur des transactions plus exotiques
(transferts groupés, wrap/unwrap SOL isolé, etc.).
"""

import asyncio
import json
import logging
import aiohttp
import websockets

import config
import rpc_client

log = logging.getLogger("copytrade")


class CopyTradeListener:
    def __init__(self, data_store, on_buy, on_sell):
        """
        data_store: instance de monitoring_list.DataStore
        on_buy: async(wallet_address: str, token_mint: str, signature: str)
        on_sell: async(wallet_address: str, token_mint: str, signature: str)
        """
        self.data_store = data_store
        self.on_buy = on_buy
        self.on_sell = on_sell
        self._running = False
        self._ws = None
        self._subscribed_wallets = set()
        self._sub_id_to_wallet = {}

    def _get_tracked_wallets(self) -> set:
        """
        Retourne l'ensemble des wallets à surveiller en temps réel : mode
        track_buy/track_sell (copy trading classique) ET buy_on_dev_sell
        (on doit détecter la VENTE du dev sur son propre token pour racheter
        juste après — voir main.py on_copytrade_sell).
        """
        wallets = set()
        for address, entry in self.data_store.state.get("monitored_dev_wallets", {}).items():
            if entry.get("mode") in ("track_buy", "track_sell", "buy_on_dev_sell"):
                wallets.add(address)
        return wallets

    async def start(self):
        if not config.HELIUS_WS_URL:
            log.warning("Aucune clé Helius configurée — copytrade listener ne démarre pas.")
            return

        self._running = True
        while self._running:
            try:
                await self._run_connection()
            except Exception as e:
                log.error(f"Connexion copytrade WebSocket perdue ({e}), reconnexion dans 5s...")
                await asyncio.sleep(5)

    async def stop(self):
        self._running = False

    async def _run_connection(self):
        async with websockets.connect(config.HELIUS_WS_URL, ping_interval=20) as ws:
            self._ws = ws
            self._subscribed_wallets = set()
            self._sub_id_to_wallet = {}

            await self._sync_subscriptions()
            log.info("✅ Copytrade listener connecté — synchronisation des wallets suivis toutes les 30s.")

            sync_task = asyncio.create_task(self._periodic_sync())
            try:
                async for message in ws:
                    await self._handle_message(message)
            finally:
                sync_task.cancel()

    async def _periodic_sync(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self._sync_subscriptions()
            except Exception as e:
                log.debug(f"Erreur sync copytrade: {e}")

    async def _sync_subscriptions(self):
        """Ajoute les souscriptions pour les nouveaux wallets track_buy/track_sell.
        Note : les wallets retirés du monitoring restent souscrits côté RPC
        jusqu'à la reconnexion suivante (pas d'unsubscribe explicite ici) —
        sans impact fonctionnel, juste une souscription en trop temporaire."""
        current = self._get_tracked_wallets()
        new_wallets = current - self._subscribed_wallets

        for wallet in new_wallets:
            sub_msg = {
                "jsonrpc": "2.0",
                "id": len(self._subscribed_wallets) + 1,
                "method": "logsSubscribe",
                "params": [{"mentions": [wallet]}, {"commitment": "processed"}],
            }
            await self._ws.send(json.dumps(sub_msg))
            self._subscribed_wallets.add(wallet)

        if new_wallets:
            log.info(f"🔔 {len(new_wallets)} nouveau(x) wallet(s) souscrit(s) en copytrade.")

    async def _handle_message(self, message: str):
        try:
            data = json.loads(message)

            # Réponse à une souscription (contient le sub id -> pas de "params")
            if "result" in data and "id" in data:
                return

            result = data.get("params", {}).get("result", {})
            value = result.get("value", {})
            signature = value.get("signature")
            logs = value.get("logs", [])

            if not signature:
                return

            # On ne traite que les swaps (achat/vente), pas les autres tx du wallet
            if not any("swap" in l.lower() or "Instruction: Buy" in l or "Instruction: Sell" in l for l in logs):
                return

            await self._process_transaction(signature)
        except Exception as e:
            log.debug(f"Erreur traitement message copytrade: {e}")

    async def _process_transaction(self, signature: str):
        parsed = await self._fetch_parsed_transaction(signature)
        if not parsed:
            return

        action_info = self._classify_transaction(parsed)
        if not action_info:
            return

        wallet = action_info["wallet"]
        if wallet not in self._subscribed_wallets:
            return

        if action_info["action"] == "buy" and self.on_buy:
            await self.on_buy(wallet, action_info["token_mint"], signature)
        elif action_info["action"] == "sell" and self.on_sell:
            await self.on_sell(wallet, action_info["token_mint"], signature)

    async def _fetch_parsed_transaction(self, signature: str) -> dict:
        """CORRIGÉ : même fix que websocket_listener.py — session isolée sans
        fix DNS ni limiteur de débit, remplacée par le connecteur/limiteur
        partagés de rpc_client.py."""
        if not config.HELIUS_API_KEY:
            return {}
        try:
            await rpc_client._rate_limiter.wait_if_needed()
            async with aiohttp.ClientSession(connector=rpc_client.get_http_connector(), connector_owner=False) as session:
                async with session.post(
                    config.HELIUS_PARSE_TX_URL,
                    json={"transactions": [signature]},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return {}
                    data = await resp.json()
                    return data[0] if data else {}
        except Exception as e:
            log.debug(f"Erreur fetch tx copytrade {signature}: {e}")
            return {}

    def _classify_transaction(self, parsed: dict) -> dict:
        """
        Détermine si la transaction est un achat ou une vente pour l'un des
        wallets suivis, et quel token est concerné. Voir la limite documentée
        en tête de fichier.
        """
        token_transfers = parsed.get("tokenTransfers", [])

        for tt in token_transfers:
            mint = tt.get("mint")
            if not mint:
                continue

            to_account = tt.get("toUserAccount")
            from_account = tt.get("fromUserAccount")

            if to_account in self._subscribed_wallets:
                return {"wallet": to_account, "action": "buy", "token_mint": mint}
            if from_account in self._subscribed_wallets:
                return {"wallet": from_account, "action": "sell", "token_mint": mint}

        return None
