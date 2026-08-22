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
import aiohttp
import websockets

import config
import rpc_client
import wallet

log = logging.getLogger("detection")


class NewTokenListener:
    """
    Écoute les créations de tokens Pump.fun en temps réel et notifie
    un callback asynchrone pour chaque nouveau token détecté.
    """

    def __init__(self, on_new_token):
        """
        on_new_token: fonction async(dict) appelée pour chaque nouveau token.
            Le dict contient : {
                "token_mint": str,
                "dev_address": str,
                "signature": str,
                "raw": dict  # transaction parsée complète (Helius)
            }
        """
        self.on_new_token = on_new_token
        self._running = False

    async def start(self):
        if not config.HELIUS_WS_URL:
            log.warning(
                "Aucune clé Helius configurée — le WebSocket ne peut pas démarrer. "
                "Ajoute HELIUS_API_KEY dans tes variables d'environnement."
            )
            return

        self._running = True
        while self._running:
            try:
                await self._listen_once()
            except Exception as e:
                log.error(f"Connexion WebSocket perdue ({e}), reconnexion dans 5s...")
                await asyncio.sleep(5)

    async def stop(self):
        self._running = False

    async def _listen_once(self):
        async with websockets.connect(config.HELIUS_WS_URL, ping_interval=20) as ws:
            # Abonnement aux logs mentionnant le programme Pump.fun
            subscribe_msg = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [config.PUMP_FUN_PROGRAM_ID]},
                    {"commitment": "processed"},
                ],
            }
            await ws.send(json.dumps(subscribe_msg))
            log.info("✅ Abonné aux logs Pump.fun — écoute en cours...")

            async for message in ws:
                try:
                    data = json.loads(message)
                    result = data.get("params", {}).get("result", {})
                    value = result.get("value", {})
                    signature = value.get("signature")
                    logs = value.get("logs", [])

                    if not signature:
                        continue

                    # On ne traite que les logs qui indiquent une INSTRUCTION de création.
                    # Pump.fun log généralement "Instruction: Create" pour une création de token.
                    if not any("Instruction: Create" in l for l in logs):
                        continue

                    await self._handle_creation(signature)

                except Exception as e:
                    log.debug(f"Erreur traitement message WS: {e}")

    async def _handle_creation(self, signature: str):
        """Récupère les détails parsés de la transaction et notifie le callback."""
        parsed = await self._fetch_parsed_transaction(signature)
        if not parsed:
            return

        token_mint, dev_address = self._extract_creation_info(parsed)
        if not token_mint or not dev_address:
            log.debug(f"Création détectée mais impossible d'extraire mint/dev pour {signature}")
            return

        await self.on_new_token({
            "token_mint": token_mint,
            "dev_address": dev_address,
            "signature": signature,
            "creation_slot": parsed.get("slot"),  # pour la télémétrie de vitesse bloc+0/bloc+N
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
                        log.debug(f"Helius parse error {resp.status} pour {signature}")
                        return {}
                    data = await resp.json()
                    return data[0] if data else {}
        except Exception as e:
            log.debug(f"Erreur fetch transaction parsée {signature}: {e}")
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
