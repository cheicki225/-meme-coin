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
   en fonction de monitored_dev_wallets (poll toutes les
   config.COPYTRADE_SYNC_INTERVAL_S secondes, 10s par défaut)
2. Dès qu'une transaction du wallet arrive, on récupère sa version parsée
   (API Enhanced Transactions Helius) et on détermine si c'est un ACHAT
   ou une VENTE via les tokenTransfers/nativeTransfers
3. On déclenche le callback approprié (on_buy / on_sell)

AJOUTÉ suite à une demande explicite : surveille maintenant AUSSI les
wallets en mode "track_creation" (Ruggeur), pas juste track_buy/track_sell/
buy_on_dev_sell — pour détecter un TRANSFERT SOL important (≥
config.DEV_SOL_TRANSFER_ALERT_PCT % du solde) vers une autre adresse,
signal fort qu'un dev encaisse et se prépare à disparaître. Voir
_check_large_sol_transfer().

LIMITE CONNUE : la classification achat/vente vérifie qu'un mouvement SOL
correspondant accompagne le token dans la même transaction (voir
_classify_transaction, corrigé suite à un vrai signalement — un simple
dépôt/retrait de token sans échange SOL n'est plus compté comme achat/
vente). Reste néanmoins une heuristique : ne couvre pas les cas où le SOL
transite par un compte intermédiaire (wrap/unwrap isolé, certains routeurs
d'agrégation) plutôt qu'un nativeTransfer direct sur le wallet lui-même.
"""

import asyncio
import json
import logging
import aiohttp
import websockets

import config
import rpc_client
import wallet

log = logging.getLogger("copytrade")


class CopyTradeListener:
    def __init__(self, data_store, on_buy, on_sell, on_large_sol_transfer=None, on_withdrawal=None):
        """
        data_store: instance de monitoring_list.DataStore
        on_buy: async(wallet_address: str, token_mint: str, signature: str)
        on_sell: async(wallet_address: str, token_mint: str, signature: str)
        on_large_sol_transfer: async(wallet_address: str, destination: str,
            amount_sol: float, pct_of_balance: float) — devs uniquement,
            seuil en % du solde (config.DEV_SOL_TRANSFER_ALERT_PCT).
        on_withdrawal: async(wallet_address: str, destination: str,
            amount_sol: float) — AJOUTÉ, TOUS les wallets surveillés,
            seuil en montant SOL absolu (config.WITHDRAWAL_ALERT_MIN_SOL),
            optionnel (None = désactivé silencieusement).
        """
        self.data_store = data_store
        self.on_buy = on_buy
        self.on_sell = on_sell
        self.on_large_sol_transfer = on_large_sol_transfer
        self.on_withdrawal = on_withdrawal
        self._running = False
        self._ws = None
        self._subscribed_wallets = set()
        self._sub_id_to_wallet = {}
        self._creation_mode_wallets = set()  # sous-ensemble surveillé pour les transferts SOL (seuil %)

    def _get_tracked_wallets(self) -> set:
        """
        Retourne l'ensemble des wallets à surveiller en temps réel : mode
        track_buy/track_sell (copy trading classique), buy_on_dev_sell
        (on doit détecter la VENTE du dev sur son propre token pour racheter
        juste après — voir main.py on_copytrade_sell), ET track_creation
        (Ruggeur — AJOUTÉ pour la détection de transfert SOL important,
        indépendante de la logique achat/vente).
        """
        wallets = set()
        self._creation_mode_wallets = set()
        for address, entry in self.data_store.state.get("monitored_dev_wallets", {}).items():
            mode = entry.get("mode", "track_creation")
            if mode in ("track_buy", "track_sell", "buy_on_dev_sell"):
                wallets.add(address)
            elif mode == "track_creation":
                wallets.add(address)
                self._creation_mode_wallets.add(address)
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
            log.info(f"✅ Copytrade listener connecté — synchronisation des wallets suivis toutes les {config.COPYTRADE_SYNC_INTERVAL_S}s.")

            sync_task = asyncio.create_task(self._periodic_sync())
            try:
                async for message in ws:
                    await self._handle_message(message)
            finally:
                sync_task.cancel()

    async def _periodic_sync(self):
        while True:
            await asyncio.sleep(config.COPYTRADE_SYNC_INTERVAL_S)
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

            # CORRIGÉ suite à l'ajout de la détection de transfert SOL : un
            # simple transfert SOL natif (System Program) n'émet PAS les
            # mots-clés "swap"/"Buy"/"Sell" dans ses logs — l'ancien filtre
            # l'aurait ignoré silencieusement. Si des wallets en mode
            # track_creation sont surveillés, on traite TOUT (pas de filtre
            # par mots-clés), puisqu'on ne peut pas deviner à l'avance si
            # c'est un transfert pertinent sans décoder la transaction.
            is_swap_related = any(
                "swap" in l.lower() or "Instruction: Buy" in l or "Instruction: Sell" in l for l in logs
            )
            # AJOUTÉ : l'alerte retrait concerne TOUS les wallets surveillés,
            # pas seulement ceux en mode track_creation — donc on traite tout
            # dès que la fonctionnalité est active et qu'on a des wallets
            # souscrits, pas juste _creation_mode_wallets.
            needs_full_scan = bool(self._creation_mode_wallets) or (
                self.on_withdrawal and self._subscribed_wallets
                and self.data_store.get_withdrawal_alert_settings().get("enabled", True)
            )
            if not is_swap_related and not needs_full_scan:
                return

            await self._process_transaction(signature)
        except Exception as e:
            log.debug(f"Erreur traitement message copytrade: {e}")

    async def _process_transaction(self, signature: str):
        parsed = await self._fetch_parsed_transaction(signature)
        if not parsed:
            return

        # AJOUTÉ : vérification du transfert SOL important, indépendante de
        # la classification achat/vente ci-dessous — un dev peut très bien
        # transférer son SOL SANS que ce soit un swap.
        if self._creation_mode_wallets:
            await self._check_large_sol_transfer(parsed, signature)

        # AJOUTÉ suite à une demande explicite : alerte retrait SOL, TOUS
        # wallets surveillés (pas juste track_creation), seuil en montant
        # absolu plutôt qu'en % du solde.
        if self.on_withdrawal:
            await self._check_withdrawal(parsed, signature)

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

    async def _check_large_sol_transfer(self, parsed: dict, signature: str):
        """
        AJOUTÉ suite à une demande explicite : détecte un transfert SOL
        sortant représentant ≥ config.DEV_SOL_TRANSFER_ALERT_PCT % du solde
        du wallet, pour un dev surveillé en mode track_creation. Le solde
        AVANT le transfert est reconstruit par approximation (solde actuel
        + montant transféré) — précis à l'exception mineure des frais de
        transaction, négligeables pour ce calcul.
        """
        if not self.on_large_sol_transfer:
            return

        native_transfers = parsed.get("nativeTransfers", []) or []
        for nt in native_transfers:
            from_account = nt.get("fromUserAccount")
            to_account = nt.get("toUserAccount")
            amount_lamports = nt.get("amount", 0)

            if from_account not in self._creation_mode_wallets or not amount_lamports:
                continue

            amount_sol = amount_lamports / 1_000_000_000
            try:
                current_balance = await wallet.get_sol_balance_of(from_account)
            except Exception as e:
                log.debug(f"Erreur lecture solde pour {from_account}: {e}")
                continue

            prior_balance_estimate = current_balance + amount_sol
            if prior_balance_estimate <= 0:
                continue

            pct_transferred = (amount_sol / prior_balance_estimate) * 100
            if pct_transferred >= config.DEV_SOL_TRANSFER_ALERT_PCT:
                log.info(
                    f"💸 Transfert SOL important détecté : {from_account[:8]}... a envoyé "
                    f"{amount_sol:.4f} SOL ({pct_transferred:.0f}% de son solde) vers {to_account[:8]}..."
                )
                await self.on_large_sol_transfer(from_account, to_account, amount_sol, pct_transferred)

    async def _check_withdrawal(self, parsed: dict, signature: str):
        """
        AJOUTÉ suite à une demande explicite : alerte sur TOUT retrait SOL
        (wallet surveillé qui envoie du SOL vers une autre adresse) dépassant
        un montant ABSOLU minimum — pour TOUS les wallets surveillés
        (Ruggeur et Copy Trading confondus), pas seulement les devs comme
        _check_large_sol_transfer (qui lui raisonne en % du solde).
        Aucune vérification de solde ici, juste "a-t-il envoyé au moins
        X SOL ?" — plus simple, et volontairement indépendant du système à
        90% déjà en place.
        """
        settings = self.data_store.get_withdrawal_alert_settings()
        if not settings.get("enabled", True):
            return

        min_sol = settings.get("min_sol", config.WITHDRAWAL_ALERT_MIN_SOL)
        native_transfers = parsed.get("nativeTransfers", []) or []

        for nt in native_transfers:
            from_account = nt.get("fromUserAccount")
            to_account = nt.get("toUserAccount")
            amount_lamports = nt.get("amount", 0)

            if from_account not in self._subscribed_wallets or not amount_lamports:
                continue

            amount_sol = amount_lamports / 1_000_000_000
            if amount_sol < min_sol:
                continue

            log.info(f"📤 Retrait SOL détecté : {from_account[:8]}... a envoyé {amount_sol:.4f} SOL vers {to_account[:8]}...")
            await self.on_withdrawal(from_account, to_account, amount_sol)

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
        wallets suivis, et quel token est concerné.

        CORRIGÉ suite à une question explicite ("dépôt/retrait ou achat/
        vente ?") : l'ancienne version se basait UNIQUEMENT sur le sens du
        token (reçu = achat, envoyé = vente), sans jamais vérifier qu'un
        vrai échange contre du SOL avait eu lieu. Un simple DÉPÔT de token
        (transfert reçu, cadeau, airdrop, mouvement entre les propres
        wallets du trader — pas un vrai trade) aurait été classé "achat" à
        tort, et pareil pour un simple RETRAIT classé "vente" à tort.
        Vérifie maintenant qu'un mouvement SOL correspondant (dans le sens
        opposé, sur le MÊME wallet, dans la MÊME transaction) existe bien
        avant de conclure à un vrai achat/vente — sinon la transaction est
        ignorée (ni achat, ni vente, ni dépôt/retrait suivi séparément).
        """
        token_transfers = parsed.get("tokenTransfers", []) or []
        native_transfers = parsed.get("nativeTransfers", []) or []

        for tt in token_transfers:
            mint = tt.get("mint")
            if not mint:
                continue

            to_account = tt.get("toUserAccount")
            from_account = tt.get("fromUserAccount")

            if to_account in self._subscribed_wallets:
                # Achat potentiel — confirme qu'un mouvement SOL SORTANT de
                # ce wallet existe bien dans la même transaction.
                has_sol_out = any(
                    nt.get("fromUserAccount") == to_account and nt.get("amount", 0) > 0
                    for nt in native_transfers
                )
                if has_sol_out:
                    return {"wallet": to_account, "action": "buy", "token_mint": mint}
            if from_account in self._subscribed_wallets:
                # Vente potentielle — confirme qu'un mouvement SOL ENTRANT
                # vers ce wallet existe bien dans la même transaction.
                has_sol_in = any(
                    nt.get("toUserAccount") == from_account and nt.get("amount", 0) > 0
                    for nt in native_transfers
                )
                if has_sol_in:
                    return {"wallet": from_account, "action": "sell", "token_mint": mint}

        return None
