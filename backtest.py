"""
════════════════════════════════════════════════════════════════
MOTEUR DE BACKTEST — Calcul de rentabilité sur les derniers tokens
════════════════════════════════════════════════════════════════
Étape 4 de la vidéo : pour chaque token passé d'un dev (ou d'un wallet
suivi en copy trade), on simule "on aurait acheté à la fin de la 1ère
bougie, avec TP +100% / SL -30%" et on calcule le résultat cumulé.

Utilise l'API DexScreener (gratuite, sans clé) pour récupérer
l'historique de prix (paires OHLC) de chaque token.
"""

import asyncio
import base64
import logging
import struct
import time

import aiohttp
from solders.pubkey import Pubkey

import config
import rpc_client

log = logging.getLogger("backtest")

DEXSCREENER_PAIRS_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"

# ════════════════════════════════════════════════════════════════
# TAUX DE CHANGE SOL/USD — EN DIRECT, PAS UNE CONSTANTE
# ════════════════════════════════════════════════════════════════
# CORRIGÉ suite à un vrai bug trouvé (repéré via une incohérence visible sur
# Axiom : un "Market cap: 5221$" affiché au buy, alors que l'ATH réel du
# token sur tout son historique n'a jamais dépassé 3.49K$). Cause :
# config.SOL_USD_RATE était une CONSTANTE STATIQUE (150$ par défaut, jamais
# mise à jour — déjà signalée comme TODO dans config.py), utilisée pour
# convertir en $ tout prix/market cap calculé on-chain (bonding curve, voir
# get_bonding_curve_price ci-dessous). Le SOL réel valait ~76$ au moment du
# diagnostic (vérifié en direct) — un écart de ~2x qui gonflait le market
# cap d'entrée ET le coût de base ($) de chaque position. Pire : le suivi
# de la position ENSUITE (_monitor_position dans paper_trader.py, via
# _get_pair_data) utilise lui un prix DexScreener réel — donc le calcul du
# PnL comparait une base d'entrée gonflée à une valeur actuelle réelle,
# rendant chaque perte affichée bien pire que la perte réelle (cas
# concret : -58.7% affiché pour une perte réelle nettement plus faible).
#
# Fix : réutilise _get_pair_data (plus bas dans ce fichier) sur le mint
# natif du SOL — même source DexScreener que tout le reste du bot, aucune
# nouvelle dépendance externe — avec un cache court pour éviter un appel
# réseau à chaque conversion. Repli en cascade sur la dernière valeur
# connue puis sur config.SOL_USD_RATE uniquement si DexScreener est
# injoignable et qu'aucune valeur n'a jamais été mise en cache (ex: tout
# premier appel juste après un redémarrage, avant le premier succès).
_sol_price_cache = {"rate": None, "ts": 0.0}
SOL_PRICE_CACHE_TTL_S = 300  # 5 min — un taux SOL/USD ne bouge pas assez vite pour justifier plus fréquent


async def get_sol_usd_rate() -> float:
    """
    Retourne le taux SOL/USD actuel, rafraîchi au maximum toutes les
    SOL_PRICE_CACHE_TTL_S secondes via DexScreener (mint natif du SOL,
    config.SOL_MINT). Ne lève jamais d'exception — voir le repli en
    cascade documenté ci-dessus.

    CORRIGÉ suite à un vrai bug trouvé (repéré via un "Market cap: 1$"
    absurde sur une notification d'achat) : la première version réutilisait
    _get_pair_data() telle quelle, qui renvoie pairs[0] sans distinguer si
    le token demandé est le baseToken ou le quoteToken de la paire. Or le
    SOL est très majoritairement QUOTE token dans les paires que DexScreener
    renvoie pour son propre mint (convention "MEMECOIN/SOL", pas
    "SOL/MEMECOIN") — et priceUsd d'une paire DexScreener correspond
    TOUJOURS au baseToken, jamais au quoteToken. pairs[0] pouvait donc être
    le prix d'un memecoin random appairé au SOL, pas le prix du SOL
    lui-même. Ne garde maintenant QUE les paires où le SOL est bien le
    baseToken, puis prend la plus liquide parmi elles (évite une paire
    exotique/peu liquide qui fausserait le prix).
    """
    now = time.time()
    if _sol_price_cache["rate"] is not None and (now - _sol_price_cache["ts"]) < SOL_PRICE_CACHE_TTL_S:
        return _sol_price_cache["rate"]

    try:
        url = DEXSCREENER_PAIRS_URL.format(address=config.SOL_MINT)
        async with aiohttp.ClientSession(connector=rpc_client.get_http_connector(), connector_owner=False) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs") or []
                    sol_base_pairs = [p for p in pairs if (p.get("baseToken") or {}).get("address") == config.SOL_MINT]
                    if sol_base_pairs:
                        best_pair = max(sol_base_pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)
                        price = float(best_pair.get("priceUsd", 0) or 0)
                        if price > 0:
                            _sol_price_cache["rate"] = price
                            _sol_price_cache["ts"] = now
                            log.info(f"💱 Taux SOL/USD rafraîchi : {price:.2f}$ (source DexScreener, paire {best_pair.get('dexId', '?')})")
                            return price
                    log.warning("⚠️ Taux SOL/USD — aucune paire avec SOL en baseToken trouvée chez DexScreener.")
    except Exception as e:
        log.warning(f"⚠️ Taux SOL/USD — échec de rafraîchissement DexScreener : {e}")

    if _sol_price_cache["rate"] is not None:
        return _sol_price_cache["rate"]  # dernière valeur connue, même périmée — mieux qu'une constante figée depuis des mois
    log.warning(
        f"⚠️ Taux SOL/USD — aucune valeur en cache, repli sur config.SOL_USD_RATE "
        f"({config.SOL_USD_RATE}$, potentiellement obsolète)."
    )
    return config.SOL_USD_RATE


# ════════════════════════════════════════════════════════════════
# PRIX EN DIRECT DEPUIS LA BONDING CURVE ON-CHAIN
# ════════════════════════════════════════════════════════════════
# AJOUTÉ suite à un vrai échec d'achat signalé : "Buy Failed - prix
# d'entrée indisponible" sur des tokens tout juste créés. Confirmé par une
# recherche externe : DexScreener (source utilisée par _get_pair_data plus
# bas) n'indexe PAS un token Pump.fun tant qu'il est sur la bonding curve —
# seulement après sa migration vers PumpSwap. Un simple retry ne change
# rien à ça, DexScreener n'a AUCUNE donnée pour ce token, quel que soit le
# temps d'attente. Cette fonction lit directement le compte bonding curve
# du token sur la blockchain (RPC standard, comme le fait déjà
# wallet_history.py pour les créations) — disponible dès T+0, immédiatement
# à la création du token, sans dépendre d'aucun indexeur tiers.
#
# Layout du compte (confirmé via la doc officielle pump-fun/pump-public-docs
# et vérifié indépendamment sur docs.rs — struct BondingCurveAccount) :
#   offset  0 (8 octets)  : discriminator Anchor
#   offset  8 (8 octets)  : virtual_token_reserves (u64)
#   offset 16 (8 octets)  : virtual_sol_reserves (u64)
#   offset 24 (8 octets)  : real_token_reserves (u64)
#   offset 32 (8 octets)  : real_sol_reserves (u64)
#   offset 40 (8 octets)  : token_total_supply (u64)
#   offset 48 (1 octet)   : complete (bool)
_PUMP_FUN_PROGRAM_PUBKEY = Pubkey.from_string(config.PUMP_FUN_PROGRAM_ID)


def _get_bonding_curve_address(mint: str) -> str:
    """PDA dérivée de ["bonding-curve", mint] — même formule que le
    programme Pump.fun lui-même (confirmé via pump-public-docs)."""
    mint_pubkey = Pubkey.from_string(mint)
    pda, _bump = Pubkey.find_program_address([b"bonding-curve", bytes(mint_pubkey)], _PUMP_FUN_PROGRAM_PUBKEY)
    return str(pda)


def _decode_bonding_curve_bytes(raw: bytes) -> dict:
    """
    Décodage brut d'un compte bonding curve Pump.fun déjà en mémoire (bytes),
    SANS aucun appel réseau — extrait de get_bonding_curve_price pour être
    réutilisé tel quel par la surveillance de position en WebSocket
    (position_price_stream.py), qui reçoit déjà les données du compte
    poussées directement par accountSubscribe, sans avoir besoin d'un appel
    getAccountInfo séparé pour chaque mise à jour.

    Retourne {"virtual_token_reserves": int, "virtual_sol_reserves": int,
    "complete": bool} ou {} si le buffer est invalide/trop court.
    """
    try:
        virtual_token_reserves = struct.unpack_from("<Q", raw, 8)[0]
        virtual_sol_reserves = struct.unpack_from("<Q", raw, 16)[0]
        complete = raw[48] != 0
    except (IndexError, TypeError, struct.error):
        return {}

    if virtual_token_reserves <= 0 or virtual_sol_reserves <= 0:
        return {}

    return {
        "virtual_token_reserves": virtual_token_reserves,
        "virtual_sol_reserves": virtual_sol_reserves,
        "complete": complete,
    }


async def get_bonding_curve_price(token_mint: str, retries: int = 2, retry_delay_s: float = 1.5) -> dict:
    """
    Lit le prix EN DIRECT depuis le compte bonding curve on-chain.
    Contrairement à _get_pair_data (DexScreener), fonctionne dès la
    création du token — aucun délai d'indexation.

    CORRIGÉ suite à un vrai échec observé en conditions réelles : un achat
    en copytrade détecté 37ms après la création du compte bonding curve
    échouait avec "value: None" — pas une erreur RPC, le compte n'avait
    simplement pas encore eu le temps de se propager sur le nœud RPC
    interrogé (latence de réplication normale, pas un bug). Réessaie
    maintenant une fois de plus avant d'abandonner, avec un court délai.

    Retourne {"price_sol": float, "market_cap_usd": float, "complete": bool}
    ou {} si le compte n'existe pas (adresse invalide, RPC en échec) ou si
    les réserves sont à zéro (curve tout juste initialisée, cas limite).
    Le champ "complete"=True signale un token déjà migré — dans ce cas,
    repasser sur _get_pair_data (DexScreener), qui a de meilleures données
    post-migration (liquidité, volume réel du pool AMM).
    """
    try:
        bonding_curve_address = _get_bonding_curve_address(token_mint)
    except Exception as e:
        log.warning(f"⚠️ Bonding curve — erreur dérivation PDA pour {token_mint}: {e}")
        return {}

    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
        # CORRIGÉ suite à un vrai échec persistant, diagnostiqué précisément
        # via les logs : aucun niveau de "commitment" n'était précisé, donc
        # l'appel utilisait le défaut du nœud RPC — presque toujours
        # "finalized", le plus LENT (attend la finalisation complète du
        # bloc, souvent plusieurs secondes). Sur un token détecté quasiment
        # au même instant que sa création (17ms d'écart observé en
        # conditions réelles), c'était structurellement impossible à voir
        # même après plusieurs tentatives. "confirmed" est un bon compromis
        # vitesse/fiabilité pour un bot de sniping — visible en général en
        # moins d'une seconde, contrairement à "finalized".
        "params": [bonding_curve_address, {"encoding": "base64", "commitment": "confirmed"}],
    }

    value = None
    for attempt in range(retries + 1):
        result = await rpc_client.rpc_post(payload)

        # CORRIGÉ — bug trouvé en repassant sur ce code suite à un échec
        # persistant : rpc_client.rpc_post() retourne DÉJÀ le champ "result"
        # déballé (voir son docstring : "Retourne le champ 'result' de la
        # réponse"), pas l'enveloppe JSON-RPC complète. Le code cherchait donc
        # result["result"]["value"] — une clé "result" qui n'existe plus à ce
        # niveau, puisqu'elle a déjà été retirée par rpc_post(). "value" était
        # donc TOUJOURS vide, silencieusement, depuis le début — la vraie cause
        # de "Buy Failed - prix d'entrée indisponible" qui persistait malgré la
        # lecture on-chain censée le corriger.
        if not result:
            log.warning(f"⚠️ Bonding curve — échec RPC pour {token_mint} (essai {attempt + 1}/{retries + 1})")
        else:
            value = result.get("value")
            if value and value.get("data"):
                break  # trouvé, pas besoin de réessayer
            log.warning(f"⚠️ Bonding curve — compte introuvable pour {token_mint} "
                        f"(essai {attempt + 1}/{retries + 1}, adresse {bonding_curve_address})")
            value = None

        if attempt < retries:
            await asyncio.sleep(retry_delay_s)

    if not value:
        return {}

    try:
        raw = base64.b64decode(value["data"][0])
    except (KeyError, IndexError, TypeError) as e:
        log.warning(f"⚠️ Bonding curve — erreur décodage base64 pour {token_mint}: {e}")
        return {}

    decoded = _decode_bonding_curve_bytes(raw)
    if not decoded:
        return {}

    return await _finalize_bonding_curve_price(decoded)


async def _finalize_bonding_curve_price(decoded: dict) -> dict:
    """
    Convertit {"virtual_token_reserves", "virtual_sol_reserves", "complete"}
    (sortie de _decode_bonding_curve_bytes) en {"price_sol", "market_cap_usd",
    "complete"} — extrait pour être partagé entre get_bonding_curve_price
    (RPC classique) et position_price_stream.py (push WebSocket).
    """
    # virtual_sol_reserves en lamports (9 décimales), virtual_token_reserves
    # en unités brutes du token (6 décimales, standard SPL/Pump.fun).
    price_sol = (decoded["virtual_sol_reserves"] / 1_000_000_000) / (decoded["virtual_token_reserves"] / 1_000_000)
    sol_usd_rate = await get_sol_usd_rate()
    price_usd = price_sol * sol_usd_rate
    market_cap_usd = price_usd * PUMPFUN_STANDARD_TOTAL_SUPPLY
    return {"price_sol": price_sol, "price_usd": price_usd, "market_cap_usd": market_cap_usd, "complete": decoded["complete"]}


async def get_live_price_and_market_cap(token_mint: str, need_pair_data: bool = False) -> dict:
    """
    CORRIGÉ suite à un vrai bug trouvé (position jamais coupée automatiquement
    ET boutons de vente manuelle/Refresh PnL montrant 0% de gain, sur un
    token resté sur la bonding curve) : open_position() utilisait déjà la
    priorité "bonding curve on-chain d'abord, DexScreener en repli" pour le
    PRIX D'ACHAT, mais _monitor_position (surveillance continue),
    _quick_sell_by_mint (vente manuelle) et _refresh_pnl (bouton Refresh)
    utilisaient TOUS uniquement _get_pair_data (DexScreener) — qui
    n'indexe JAMAIS un token Pump.fun tant qu'il reste sur la bonding
    curve (voir le docstring de get_bonding_curve_price). Résultat concret :
    une position sur un token qui ne migre jamais (la majorité des entrées
    copy trade, largement sous le seuil de migration ~69K$) recevait
    price=0 à CHAQUE sondage de _monitor_position, qui "continue"
    immédiatement — sautant TOUTE la logique de sortie (TP, trailing SL,
    mc trailing, profit trail, no-activity-sell) pendant toute la durée de
    vie de la position, celle-ci restant ouverte indéfiniment sans aucune
    protection réelle. La vente manuelle et le Refresh PnL retombaient eux
    silencieusement sur position["entry_price"] (0% affiché) au lieu du
    vrai prix, masquant un vrai gain ou une vraie perte — d'où l'impression
    que ces boutons "ne fonctionnent pas".

    Point d'entrée UNIQUE pour "quel est le prix/market cap actuel de ce
    token", réutilisé par open_position, _monitor_position,
    _quick_sell_by_mint et _refresh_pnl — même priorité partout : bonding
    curve on-chain d'abord (tant que non "complete"), DexScreener sinon.

    need_pair_data : si True, tente aussi un appel DexScreener même quand
    le prix vient de la bonding curve — utile pour des champs auxiliaires
    comme txns.m5 (no_activity_sell). Coûte un appel réseau de plus ; à
    activer seulement si l'appelant en a vraiment besoin.

    Retourne {"price": float, "market_cap": float, "pair_data": dict,
    "source": "onchain"|"dexscreener"}. pair_data peut être {} même en
    provenance "dexscreener" si DexScreener n'a rien retourné.
    """
    onchain = await get_bonding_curve_price(token_mint)
    if onchain and not onchain.get("complete"):
        pair_data = await _get_pair_data(token_mint) if need_pair_data else {}
        return {
            "price": onchain["price_sol"] * await get_sol_usd_rate(),
            "market_cap": onchain["market_cap_usd"],
            "pair_data": pair_data,
            "source": "onchain",
        }

    pair_data = await _get_pair_data(token_mint)
    return {
        "price": float(pair_data.get("priceUsd", 0) or 0),
        "market_cap": float(pair_data.get("marketCap", pair_data.get("fdv", 0)) or 0),
        "pair_data": pair_data,
        "source": "dexscreener",
    }

# ════════════════════════════════════════════════════════════════
# GECKOTERMINAL — vraies bougies OHLC historiques (gratuit, sans clé)
# ════════════════════════════════════════════════════════════════
# AJOUTÉ suite à un bug structurel identifié : le calcul "prix d'entrée vs
# prix ACTUEL" (voir backtest_token / get_detailed_trade_info plus bas) ne
# peut jamais détecter qu'un TP a été touché EN CHEMIN si le token a ensuite
# dump — un token qui pump +100% puis rug (le cas standard sur Pump.fun)
# affichera TOUJOURS un SL avec cette méthode, quel que soit le vrai
# déroulé du trade, dès qu'on relance le backtest après le crash.
# GeckoTerminal fournit de vraies bougies OHLC par pool (30 req/min, sans
# clé) — on peut donc parcourir le prix minute par minute dans l'ordre
# chronologique et voir CE QUI EST TOUCHÉ EN PREMIER, TP ou SL.
GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v2"
_SOLANA_NETWORK = "solana"


class _GeckoRateLimiter:
    """Fenêtre glissante de 60s, respecte la limite gratuite de 30 req/min
    (garde une marge de sécurité à 25/min)."""

    def __init__(self, max_per_minute: float = 25.0):
        self._max = max_per_minute
        self._calls = []
        self._lock = asyncio.Lock()

    async def wait_if_needed(self):
        async with self._lock:
            now = time.monotonic()
            self._calls = [t for t in self._calls if now - t < 60]
            if len(self._calls) >= self._max:
                sleep_for = 60 - (now - self._calls[0]) + 0.05
                await asyncio.sleep(max(sleep_for, 0))
                now = time.monotonic()
                self._calls = [t for t in self._calls if now - t < 60]
            self._calls.append(time.monotonic())


_gecko_rate_limiter = _GeckoRateLimiter()


async def _gecko_get(path: str, params: dict = None) -> dict:
    await _gecko_rate_limiter.wait_if_needed()
    url = f"{GECKOTERMINAL_BASE}{path}"
    try:
        async with aiohttp.ClientSession(connector=rpc_client.get_http_connector(), connector_owner=False) as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status != 200:
                    return {}
                return await resp.json()
    except Exception as e:
        log.debug(f"Erreur GeckoTerminal {path}: {e}")
        return {}


async def _get_pool_address(token_mint: str) -> str:
    """Trouve le pool (le plus liquide, retourné en premier par l'API) qui trade ce token."""
    data = await _gecko_get(f"/networks/{_SOLANA_NETWORK}/tokens/{token_mint}/pools")
    pools = data.get("data") or []
    if not pools:
        return None
    attrs = pools[0].get("attributes", {})
    address = attrs.get("address")
    if address:
        return address
    # Repli : l'id JSON:API est au format "solana_<adresse>"
    raw_id = pools[0].get("id", "")
    return raw_id.split("_", 1)[1] if "_" in raw_id else None


async def _get_ohlcv(pool_address: str, timeframe: str = "minute", aggregate: int = 1,
                      before_timestamp: int = None, limit: int = 1000) -> list:
    """Retourne les bougies [timestamp, open, high, low, close, volume] triées
    du plus ANCIEN au plus RÉCENT (l'API les retourne en ordre inverse)."""
    params = {"aggregate": aggregate, "limit": limit, "currency": "usd", "token": "base"}
    if before_timestamp:
        params["before_timestamp"] = before_timestamp
    data = await _gecko_get(f"/networks/{_SOLANA_NETWORK}/pools/{pool_address}/ohlcv/{timeframe}", params=params)
    try:
        candle_list = data["data"]["attributes"]["ohlcv_list"]
    except (KeyError, TypeError):
        return []
    return sorted(candle_list, key=lambda c: c[0])


async def backtest_token_pathaware(token_mint: str, entry_block_time: int,
                                    tp_pct: float = None, sl_pct: float = None) -> dict:
    """
    MÉTHODE DEMANDÉE : compare le prix d'entrée au prix MAXIMUM atteint
    après l'achat (ATH post-entrée), plutôt que de parcourir bougie par
    bougie dans l'ordre. Plus simple, et — puisque le SL est désactivé par
    défaut maintenant (config.SL_PCT=0) — exactement équivalente à un
    parcours chronologique dans le cas courant (sans SL, l'ordre n'a pas
    d'importance : soit le TP a été touché à un moment donné, soit non).

    Si un SL est explicitement fourni/réactivé (sl_pct > 0), le calcul reste
    une approximation : il vérifie séparément si le TP a été atteint (via le
    plus haut) ET si le SL a été touché (via le plus bas), sans savoir lequel
    est arrivé en premier chronologiquement — c'est la limite documentée et
    acceptée explicitement pour cette méthode.

    LIMITE (inchangée) : GeckoTerminal n'indexe le pool d'un token Pump.fun
    QU'APRÈS sa migration vers PumpSwap/Raydium (bonding curve complétée).
    La quasi-totalité des tokens analysés ici (market cap de quelques
    milliers de dollars) sont encore sur la bonding curve — cette fonction
    renvoie alors None, et l'appelant se rabat sur
    backtest_token_onchain_pathaware() (reconstruction directe on-chain).

    Retourne None si les données sont indisponibles.
    """
    tp_pct = tp_pct if tp_pct is not None else config.TP_PCT
    sl_pct = sl_pct if sl_pct is not None else config.SL_PCT

    if not entry_block_time:
        return None

    pool_address = await _get_pool_address(token_mint)
    if not pool_address:
        return None

    candles = await _get_ohlcv(pool_address, timeframe="minute", aggregate=1, limit=1000)
    if not candles:
        return None

    candles = [c for c in candles if c[0] >= entry_block_time]
    if len(candles) < 2:
        return None

    # Prix d'entrée = clôture de la 1ère bougie après l'achat (reproduit la
    # méthodologie "achat à la fin de la 1ère bougie" déjà documentée ici).
    entry_price = candles[0][4]
    if not entry_price or entry_price <= 0:
        return None

    max_price_after = max(c[2] for c in candles[1:])  # plus haut ('high') de chaque bougie
    min_price_after = min(c[3] for c in candles[1:])  # plus bas ('low') de chaque bougie
    max_gain_pct = ((max_price_after - entry_price) / entry_price) * 100
    max_drop_pct = ((min_price_after - entry_price) / entry_price) * 100  # négatif

    if max_gain_pct >= tp_pct:
        return {"token_mint": token_mint, "result_pct": tp_pct, "hit_tp": True, "hit_sl": False,
                "reason": f"TP atteint (+{tp_pct}%) [MC max après entrée: +{max_gain_pct:.0f}% — GeckoTerminal]"}
    if sl_pct and max_drop_pct <= -sl_pct:
        return {"token_mint": token_mint, "result_pct": -sl_pct, "hit_tp": False, "hit_sl": True,
                "reason": f"SL touché (-{sl_pct}%) [MC min après entrée: {max_drop_pct:.0f}% — GeckoTerminal]"}

    last_close = candles[-1][4]
    result_pct = ((last_close - entry_price) / entry_price) * 100
    return {"token_mint": token_mint, "result_pct": result_pct, "hit_tp": False, "hit_sl": False,
            "reason": f"Ni TP ni SL — MC max atteint: +{max_gain_pct:.0f}% [GeckoTerminal]"}



# ════════════════════════════════════════════════════════════════
# RECONSTRUCTION DE PRIX ON-CHAIN — fonctionne même sur la bonding curve
# ════════════════════════════════════════════════════════════════
# AJOUTÉ suite à un test réel : GeckoTerminal renvoie systématiquement
# "aucun pool" pour tout token encore sur la bonding curve (market cap
# < ~$60-90k, càd la quasi-totalité des tokens dev-snipés/copiés ici juste
# après création). backtest_token_pathaware() ne s'active donc quasiment
# jamais en pratique. Cette version décode directement chaque trade
# Pump.fun sur le mint (même approche brute que wallet_history.py — RPC
# standard, pas d'API tierce) pour reconstruire une vraie série de prix
# chronologique, y compris pendant la bonding curve.

async def _extract_trade_price(tx: dict) -> float:
    """
    Calcule le prix d'exécution (en SOL par token) d'UN trade Pump.fun à
    partir des changements de solde bruts (preBalances/postBalances +
    preTokenBalances/postTokenBalances) — ne dépend d'aucun format précis
    d'instruction (buy/buy_v2/sell/sell_v2, jamais confirmé avec certitude
    dans ce projet, voir wallet_history.get_recent_buys).

    Cherche le plus gros changement de solde de token dans la transaction
    (en valeur absolue — identifie l'une des deux parties du swap, trader
    ou compte de la bonding curve, peu importe lequel : le ratio SOL/token
    de CE compte reflète le même prix d'exécution pour les deux) et le SOL
    correspondant dépensé/reçu par le MÊME compte.
    """
    try:
        meta = tx.get("meta", {})
        pre_tb = meta.get("preTokenBalances", []) or []
        post_tb = meta.get("postTokenBalances", []) or []
        account_keys = tx["transaction"]["message"]["accountKeys"]
        pre_balances = meta.get("preBalances", [])
        post_balances = meta.get("postBalances", [])
    except (KeyError, TypeError):
        return None

    def _amounts(balances):
        result = {}
        for b in balances:
            owner = b.get("owner")
            if not owner:
                continue
            try:
                amount = float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
            except (TypeError, ValueError):
                amount = 0
            result[owner] = amount
        return result

    pre_amounts = _amounts(pre_tb)
    post_amounts = _amounts(post_tb)

    biggest_owner, biggest_delta = None, 0.0
    for owner in set(list(pre_amounts.keys()) + list(post_amounts.keys())):
        delta = post_amounts.get(owner, 0) - pre_amounts.get(owner, 0)
        if abs(delta) > abs(biggest_delta):
            biggest_owner, biggest_delta = owner, delta

    if not biggest_owner or biggest_delta == 0:
        return None

    wallet_index = None
    for i, acc in enumerate(account_keys):
        key = acc.get("pubkey") if isinstance(acc, dict) else acc
        if key == biggest_owner:
            wallet_index = i
            break

    if wallet_index is None or wallet_index >= len(pre_balances) or wallet_index >= len(post_balances):
        return None

    lamports_delta = post_balances[wallet_index] - pre_balances[wallet_index]
    sol_amount = abs(lamports_delta) / 1_000_000_000
    token_amount = abs(biggest_delta)

    if sol_amount <= 0 or token_amount <= 0:
        return None

    return sol_amount / token_amount


async def _get_price_points_after(token_mint: str, since_block_time: int,
                                   max_pages: int = 5, max_transactions: int = 150,
                                   show_progress: bool = True) -> list:
    """
    Reconstruit une série de points de prix RÉELS (block_time, prix_en_sol)
    à partir des transactions Pump.fun impliquant ce mint après
    `since_block_time`, triée par ordre chronologique CROISSANT.

    max_pages : plafond de pagination des SIGNATURES (getSignaturesForAddress,
    avant filtrage par date — un token très tradé peut avoir des milliers de
    signatures depuis sa création).
    max_transactions : plafond du nombre de transactions réellement décodées
    (getTransaction, l'appel coûteux) après filtrage par date — protection
    contre un token à volume extrême qui rendrait l'analyse trop lente.
    RÉDUIT de 400 à 150 par défaut suite à un vrai signalement (analyse de
    wallet complète = jusqu'à 20 minutes) : le rate limiter global de
    rpc_client.py plafonne le débit réel à ~5-6 req/s quel que soit le
    niveau de concurrence utilisé ici — la seule vraie façon de réduire le
    temps total est de réduire le nombre de transactions décodées. 150
    reste largement suffisant pour l'échantillonnage uniforme (voir
    plus bas) : on cherche le MC MAXIMUM atteint, pas une précision à la
    seconde près.

    show_progress : affiche une progression via print() (pas juste log.info,
    qui reste invisible dans les scripts CLI n'appelant jamais
    logging.basicConfig() — voir main.py, seul endroit du projet où c'est
    configuré). Décoder jusqu'à 400 tx à ~5-6/s (rate limiter global de
    rpc_client.py) peut prendre plus d'une minute PAR TOKEN — sans ce
    retour visuel, le script paraît figé alors qu'il tourne normalement.
    """
    import sys
    import wallet_history  # import local — évite une dépendance circulaire au chargement du module

    all_sigs = await wallet_history.get_all_signatures_paginated(token_mint, max_pages=max_pages, page_size=1000)
    if not all_sigs:
        return []

    relevant = [s for s in all_sigs if (s.get("blockTime") or 0) >= since_block_time]
    relevant.reverse()  # plus ancien -> plus récent

    if len(relevant) > max_transactions:
        # CORRIGÉ suite à un vrai écart signalé : garder les max_transactions
        # PREMIÈRES (les plus anciennes) coupait systématiquement la fin de
        # la période — or c'est souvent là que se trouve le vrai pic pour un
        # token à fort volume (ex: un pic à +230% survenu après la 400e
        # transaction depuis l'achat n'était JAMAIS vu, le calcul plafonnait
        # à ce que montraient les toutes premières transactions seulement).
        # Échantillonne maintenant UNIFORMÉMENT sur toute la période — moins
        # de résolution, mais couvre le début ET la fin, donc ne rate plus
        # un pic tardif.
        log.debug(f"{token_mint[:12]}...: {len(relevant)} tx depuis l'achat, échantillonné à {max_transactions}")
        step = len(relevant) / max_transactions
        relevant = [relevant[int(i * step)] for i in range(max_transactions)]

    if show_progress:
        print(f"      ↳ {len(relevant)} transaction(s) à décoder depuis l'achat...", flush=True)

    # CORRIGÉ suite à un vrai signalement : une analyse de wallet complète
    # (10 tokens × jusqu'à 400 tx chacun) prenait jusqu'à 20 minutes. Cause :
    # décodage 100% SÉQUENTIEL ici (un await à la fois), alors que
    # pattern_detector.py fait exactement le même genre d'opération (décoder
    # plein de transactions une par une) en LOTS CONCURRENTS depuis
    # longtemps (concurrency=5, voir find_fixed_amount_recipients). Le
    # rate limiter global de rpc_client.py régule déjà le débit réel
    # (~5-6 req/s, tous les composants confondus) — l'attente séquentielle
    # ajoutait donc une latence réseau redondante (temps d'aller-retour de
    # CHAQUE requête, l'une après l'autre) par-dessus cette régulation, sans
    # aucun bénéfice. Traite maintenant par lots de 5 en parallèle, comme
    # pattern_detector.py — le débit réel reste le même (toujours limité
    # par rpc_client.py), mais le temps d'attente réseau se chevauche au
    # lieu de s'additionner.
    concurrency = 5
    semaphore = asyncio.Semaphore(concurrency)

    async def _decode_one(sig_info):
        async with semaphore:
            tx = await wallet_history._get_raw_transaction(sig_info["signature"])
            if not tx or not wallet_history._transaction_involves_program(tx, config.PUMP_FUN_PROGRAM_ID):
                return None
            price = await _extract_trade_price(tx)
            if not price:
                return None
            return (tx.get("blockTime") or sig_info.get("blockTime"), price)

    points = []
    for i in range(0, len(relevant), concurrency):
        batch = relevant[i:i + concurrency]
        results = await asyncio.gather(*[_decode_one(s) for s in batch])
        points.extend(r for r in results if r is not None)

        if show_progress and (i + concurrency) % 20 < concurrency:
            print(f"      ↳ {min(i + concurrency, len(relevant))}/{len(relevant)} tx décodées, {len(points)} prix trouvés...", flush=True)

    return points


async def backtest_token_onchain_pathaware(token_mint: str, entry_price_sol: float, entry_block_time: int,
                                            tp_pct: float = None, sl_pct: float = None,
                                            max_transactions: int = 150) -> dict:
    """
    MÉTHODE DEMANDÉE, version on-chain (sans dépendance à une API tierce) —
    fonctionne pour un token à N'IMPORTE QUEL stade (bonding curve ou migré),
    contrairement à backtest_token_pathaware() qui dépend d'un pool indexé
    par GeckoTerminal (introuvable pour la quasi-totalité des tokens
    analysés ici, encore en bonding curve).

    Compare le prix d'entrée au prix MAXIMUM atteint parmi tous les trades
    reconstruits après l'achat (résultat = (max_après - entrée) / entrée),
    plutôt que de parcourir chaque trade dans l'ordre. Puisque le SL est
    désactivé par défaut (config.SL_PCT=0), cette simplification est
    exactement équivalente à un parcours chronologique dans le cas courant :
    sans sortie anticipée, seul "le TP a-t-il été touché à un moment donné"
    compte, peu importe l'ordre. Si un SL est réactivé (sl_pct > 0), le
    calcul vérifie séparément si le plus bas post-entrée l'a franchi, sans
    savoir si ça s'est produit avant ou après le plus haut — limite acceptée
    explicitement pour cette méthode plus simple.

    entry_price_sol doit idéalement venir du VRAI prix d'entrée (sol_spent /
    tokens_received de la transaction d'achat elle-même). Si non fourni
    (entry_price_sol=None) — AJOUTÉ pour les tokens CRÉÉS (pas achetés,
    donc sans notion de "prix payé") — dérive le prix d'entrée depuis le
    tout premier trade reconstruit après la création, comme le fait déjà
    backtest_token_pathaware() (GeckoTerminal) avec la clôture de la 1ère
    bougie. Permet d'analyser un dev-créateur avec la même méthode fiable
    que pour un trader, sans avoir besoin d'un montant "acheté".

    max_transactions : AJOUTÉ (demande explicite, réduction du coût RPC)
    — profondeur de décodage transmise telle quelle à _get_price_points_after.
    150 par défaut (précision maximale, adapté à une analyse ponctuelle
    déclenchée par l'utilisateur — "Analyse de wallet"/"Analyse de dev").
    wallet_cleanup.py passe une valeur plus basse : c'est une vérification
    AUTOMATIQUE, fréquente (jusqu'à 28 wallets × 10 trades à chaque
    passage), où le coût RPC cumulé compte plus que la précision maximale
    sur chaque trade individuel — voir wallet_cleanup._count_consecutive_losses.
    """
    tp_pct = tp_pct if tp_pct is not None else config.TP_PCT
    sl_pct = sl_pct if sl_pct is not None else config.SL_PCT

    if not entry_block_time:
        return None

    points = await _get_price_points_after(token_mint, entry_block_time, max_transactions=max_transactions)
    if not points:
        return None

    if not entry_price_sol:
        entry_price_sol = points[0][1]  # prix du tout premier trade reconstruit
    if not entry_price_sol or entry_price_sol <= 0:
        return None

    prices = [p for _, p in points]
    max_price_after = max(prices)
    min_price_after = min(prices)
    max_gain_pct = ((max_price_after - entry_price_sol) / entry_price_sol) * 100
    max_drop_pct = ((min_price_after - entry_price_sol) / entry_price_sol) * 100  # négatif

    if max_gain_pct >= tp_pct:
        return {"token_mint": token_mint, "result_pct": tp_pct, "hit_tp": True, "hit_sl": False,
                "reason": f"TP atteint (+{tp_pct}%) [MC max après entrée: +{max_gain_pct:.0f}% — on-chain]"}
    if sl_pct and max_drop_pct <= -sl_pct:
        return {"token_mint": token_mint, "result_pct": -sl_pct, "hit_tp": False, "hit_sl": True,
                "reason": f"SL touché (-{sl_pct}%) [MC min après entrée: {max_drop_pct:.0f}% — on-chain]"}

    last_price = points[-1][1]
    result_pct = ((last_price - entry_price_sol) / entry_price_sol) * 100
    return {"token_mint": token_mint, "result_pct": result_pct, "hit_tp": False, "hit_sl": False,
            "reason": f"Ni TP ni SL — MC max atteint: +{max_gain_pct:.0f}% [on-chain]"}


async def check_first_candle_filter(token_mint: str) -> dict:
    """
    Reproduit le filtre "pas de bundle à plus de 15k" des vidéos : rejette un
    token dont le prix a déjà trop monté avant qu'un sniper "normal" ait pu
    entrer, ce qui indique soit un bundle énorme, soit qu'on est déjà trop tard.

    LIMITE : DexScreener ne donne pas le market cap exact "fin de 1ère bougie"
    (equivalent à 400ms sur Solana) — on utilise le market cap actuel comme
    proxy le plus proche disponible gratuitement. Pour un filtre précis en
    production, il faudrait capturer le market cap au moment de la détection
    WebSocket elle-même (voir detection/websocket_listener.py) plutôt que de
    le reconstituer a posteriori.

    Retourne : {"passes": bool, "market_cap": float, "reason": str}
    """
    pair_data = await _get_pair_data(token_mint)
    if not pair_data:
        return {"passes": False, "market_cap": 0, "reason": "Données indisponibles"}

    market_cap = float(pair_data.get("marketCap", pair_data.get("fdv", 0)) or 0)

    if market_cap > config.MAX_FIRST_CANDLE_MARKET_CAP:
        return {
            "passes": False, "market_cap": market_cap,
            "reason": f"Market cap {market_cap:.0f}$ > limite {config.MAX_FIRST_CANDLE_MARKET_CAP:.0f}$",
        }

    return {"passes": True, "market_cap": market_cap, "reason": "OK"}


def suggest_tp_from_history(trades: list) -> float:
    """
    Suggère un TP basé sur l'historique, en suivant la méthodologie des vidéos :
    ne pas viser le point le plus haut (trop gourmand, rarement atteint deux fois),
    mais plutôt le milieu de la distribution des gains observés, avec un ratio
    gain/perte visé d'au moins 1:3 (config.BACKTEST_MIN_RATIO).

    trades: liste de résultats de backtest_token (avec "result_pct")
    Retourne le TP suggéré en %.
    """
    positive_results = sorted([t["result_pct"] for t in trades if t["result_pct"] > 0])
    if not positive_results:
        return config.TP_PCT

    mid_index = len(positive_results) // 2
    median_gain = positive_results[mid_index]

    # On ne descend jamais sous le TP minimum configuré, et on arrondit à un
    # multiple de 10% pour rester lisible (comme dans les exemples des vidéos : 100, 150, 200%)
    suggested = max(config.TP_PCT, round(median_gain / 10) * 10)
    return suggested


async def backtest_token(token_mint: str, tp_pct: float = None, sl_pct: float = None) -> dict:
    """
    Simule un trade sur un token donné avec les paramètres TP/SL fournis.
    Retourne : {
        "token_mint": str,
        "result_pct": float,   # résultat du trade en % (positif ou négatif)
        "hit_tp": bool,
        "hit_sl": bool,
        "reason": str,
    }
    """
    tp_pct = tp_pct if tp_pct is not None else config.TP_PCT
    sl_pct = sl_pct if sl_pct is not None else config.SL_PCT

    pair_data = await _get_pair_data(token_mint)
    if not pair_data:
        return {"token_mint": token_mint, "result_pct": 0.0, "hit_tp": False,
                "hit_sl": False, "reason": "Données de prix indisponibles"}

    # DexScreener ne fournit pas de vrai OHLC historique gratuit fin à la bougie.
    # On approxime le point d'entrée avec le prix le plus bas connu après création
    # (proxy pour "fin de 1ère bougie") et on compare au plus haut ('ath') et au
    # plus bas après ('atl' post-entrée) pour estimer si TP ou SL aurait été touché.
    price_now = pair_data.get("priceUsd")
    price_change_h24 = pair_data.get("priceChange", {}).get("h24", 0)

    # Approximation : si le prix a fait plus que +tp_pct% depuis le creuset connu,
    # on considère le TP atteint ; sinon on regarde la baisse max connue pour le SL.
    if price_change_h24 is None:
        return {"token_mint": token_mint, "result_pct": 0.0, "hit_tp": False,
                "hit_sl": False, "reason": "Variation de prix indisponible"}

    if price_change_h24 >= tp_pct:
        return {"token_mint": token_mint, "result_pct": tp_pct, "hit_tp": True,
                "hit_sl": False, "reason": f"TP atteint (+{tp_pct}%)"}
    elif sl_pct and price_change_h24 <= -sl_pct:
        return {"token_mint": token_mint, "result_pct": -sl_pct, "hit_tp": False,
                "hit_sl": True, "reason": f"SL touché (-{sl_pct}%)"}
    else:
        return {"token_mint": token_mint, "result_pct": price_change_h24, "hit_tp": False,
                "hit_sl": False, "reason": "Ni TP ni SL — position encore ouverte au moment du backtest"}


async def backtest_wallet(tokens: list, tp_pct: float = None, sl_pct: float = None) -> dict:
    """
    Backteste une liste de tokens (ceux créés/achetés par un wallet) et
    retourne le résultat cumulé + la décision d'ajout au monitoring.

    tokens: liste de dicts avec au moins "token_mint"

    Retourne : {
        "total_result_pct": float,
        "trades": list,
        "should_monitor": bool,   # True si ratio gain/perte >= BACKTEST_MIN_RATIO
        "ratio": float,
    }
    """
    trades = []
    total_gain = 0.0
    total_loss = 0.0
    skipped_bundle = 0

    for t in tokens[:config.BACKTEST_MIN_TOKENS]:
        # Filtre "pas de bundle à plus de 15k" avant même de backtester le token
        candle_check = await check_first_candle_filter(t["token_mint"])
        if not candle_check["passes"]:
            skipped_bundle += 1
            continue

        result = await backtest_token(t["token_mint"], tp_pct, sl_pct)
        trades.append(result)

        if result["result_pct"] > 0:
            total_gain += result["result_pct"]
        else:
            total_loss += abs(result["result_pct"])

    total_result = total_gain - total_loss
    ratio = (total_gain / total_loss) if total_loss > 0 else float("inf") if total_gain > 0 else 0.0
    suggested_tp = suggest_tp_from_history(trades)

    should_monitor = (
        len(trades) >= min(config.BACKTEST_MIN_TOKENS, len(tokens)) - skipped_bundle
        and len(trades) >= 3  # minimum absolu pour ne pas juger sur un échantillon trop petit
        and ratio >= config.BACKTEST_MIN_RATIO
        and total_result > 0
    )

    log.info(
        f"Backtest sur {len(trades)} tokens ({skipped_bundle} exclus pour bundle > "
        f"{config.MAX_FIRST_CANDLE_MARKET_CAP:.0f}$) — résultat cumulé: {total_result:+.1f}%, "
        f"ratio: {ratio:.2f}, TP suggéré: {suggested_tp:.0f}%, monitoring recommandé: {should_monitor}"
    )

    return {
        "total_result_pct": total_result,
        "trades": trades,
        "should_monitor": should_monitor,
        "ratio": ratio,
        "suggested_tp_pct": suggested_tp,
        "skipped_bundle": skipped_bundle,
    }


async def _get_pair_data(token_mint: str, retries: int = 3, retry_delay_s: float = 1.5) -> dict:
    """
    CORRIGÉ suite à un vrai échec d'achat signalé : "Buy Failed - prix
    d'entrée indisponible" survenait systématiquement sur des tokens tout
    juste créés, achetés quasi instantanément après détection
    (snipe_delay_s=0 par défaut). Le token existe bien on-chain, mais
    DexScreener n'a simplement pas encore eu le temps de l'indexer —
    quelques secondes de retard suffisent à provoquer une liste de paires
    vide. Réessaie maintenant plusieurs fois avec un court délai avant
    d'abandonner, au lieu d'un seul essai instantané.
    """
    url = DEXSCREENER_PAIRS_URL.format(address=token_mint)
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession(connector=rpc_client.get_http_connector(), connector_owner=False) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        pairs = data.get("pairs") or []
                        if pairs:
                            return pairs[0]
        except Exception as e:
            log.debug(f"Erreur DexScreener pour {token_mint} (essai {attempt + 1}/{retries}): {e}")

        if attempt < retries - 1:
            await asyncio.sleep(retry_delay_s)

    return {}


PUMPFUN_STANDARD_TOTAL_SUPPLY = 1_000_000_000  # confirmé via données réelles Mobula (totalSupply)


async def get_detailed_trade_info(token_mint: str, purchase_block_time: int = None,
                                   tp_pct: float = None, sl_pct: float = None,
                                   sol_spent: float = None, tokens_received: float = None,
                                   max_transactions: int = 150) -> dict:
    """
    Rassemble TOUTES les données disponibles pour un token — pas juste le
    résultat du backtest, mais aussi le nom, le prix, le market cap, la
    liquidité, le volume — en réutilisant les mêmes données DexScreener déjà
    récupérées par _get_pair_data, sans appel réseau supplémentaire.

    AJOUTÉ (demande explicite) : si sol_spent et tokens_received sont
    fournis (données EXACTES extraites de la transaction d'achat elle-même,
    voir wallet_history._find_native_sol_spent), calcule le VRAI market cap
    d'entrée — pas le market cap actuel, celui au moment précis de l'achat.
    Méthode : prix d'entrée = sol_spent / tokens_received (exact, on-chain),
    puis converti en $ via le prix SOL actuel (seule partie approximée —
    on n'a pas d'historique fiable du cours SOL/USD à chaque instant passé),
    puis multiplié par l'offre totale du token (déduite du ratio market
    cap actuel / prix actuel, ou repli sur 1 milliard — standard Pump.fun).

    Retourne un dict enrichi combinant le résultat de backtest_token() avec
    les métadonnées brutes. Certains champs peuvent être None si DexScreener
    ne les fournit pas pour ce token précis (normal, pas une erreur).

    max_transactions : AJOUTÉ (demande explicite, réduction du coût RPC) —
    transmis tel quel à backtest_token_onchain_pathaware (150 par défaut).
    Réduire cette valeur réduit la précision de la reconstruction on-chain
    (moins de transactions décodées après l'achat = risque de rater un pic
    de prix survenu tard) mais réduit proportionnellement le coût RPC —
    pertinent pour un appelant automatique et fréquent (wallet_cleanup),
    moins pour une analyse ponctuelle déclenchée par l'utilisateur.
    """
    pair_data = await _get_pair_data(token_mint)
    backtest_result = await backtest_token(token_mint, tp_pct=tp_pct, sl_pct=sl_pct)

    entry_market_cap_usd = None
    entry_price_sol = None
    if sol_spent and tokens_received and pair_data:
        try:
            entry_price_sol = sol_spent / tokens_received
            current_price_usd = float(pair_data.get("priceUsd") or 0)
            current_market_cap = float(pair_data.get("marketCap") or pair_data.get("fdv") or 0)
            sol_price_usd = None
            if current_price_usd > 0 and current_market_cap > 0:
                total_supply = current_market_cap / current_price_usd
                # sol_price_usd déduit du prix actuel du token en $ / en SOL
                # -- mais on n'a pas le prix actuel EN SOL directement depuis
                # DexScreener (il donne le prix en $ et la variation, pas le
                # ratio SOL). Repli : utilise priceNative si disponible.
                price_native = pair_data.get("priceNative")
                if price_native and float(price_native) > 0:
                    sol_price_usd = current_price_usd / float(price_native)
            else:
                total_supply = PUMPFUN_STANDARD_TOTAL_SUPPLY

            if sol_price_usd:
                entry_price_usd = entry_price_sol * sol_price_usd
                entry_market_cap_usd = entry_price_usd * total_supply
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # CORRIGÉ suite à un vrai problème signalé : result_pct venait de
    # priceChange.h24 de DexScreener (variation générique des dernières 24h,
    # sans lien avec LE moment précis d'achat de CE wallet), alors que
    # entry_market_cap_usd est calculé à partir du VRAI prix d'entrée —
    # deux chiffres différents affichés côte à côte comme s'ils racontaient
    # la même histoire, ce qui pouvait donner des résultats incohérents
    # (ex: MC d'entrée très supérieur au MC actuel, mais un "-2.2%" affiché
    # au lieu du vrai "-61%"). Recalcule maintenant result_pct à partir du
    # VRAI prix d'entrée (SOL) comparé au prix actuel (SOL) quand on a les
    # données pour le faire — plus précis que l'approximation DexScreener.
    # PRIORITÉ 1 — reconstruction on-chain (nouveau) : fonctionne quel que
    # soit le stade du token (bonding curve OU migré), car elle décode les
    # trades directement depuis les transactions Solana plutôt que de
    # dépendre d'un pool indexé par un service tiers. C'est la méthode la
    # plus fiable pour la quasi-totalité des tokens analysés ici (dev-sniping
    # et copy trading juste après création = quasiment toujours en bonding
    # curve, jamais migré vers un vrai pool AMM).
    # AJOUTÉ : ne dépend plus de entry_price_sol (utile pour un token CRÉÉ,
    # pas acheté — sans sol_spent/tokens_received, entry_price_sol reste
    # vide, mais backtest_token_onchain_pathaware sait maintenant dériver
    # le prix d'entrée depuis le tout premier trade si besoin).
    onchain_result = None
    if purchase_block_time:
        onchain_result = await backtest_token_onchain_pathaware(
            token_mint, entry_price_sol, purchase_block_time, tp_pct=tp_pct, sl_pct=sl_pct,
            max_transactions=max_transactions,
        )
    if onchain_result is not None:
        backtest_result = onchain_result
    else:
        # PRIORITÉ 2 — GeckoTerminal (repli) : ne fonctionne QUE si le token a
        # déjà migré vers PumpSwap/Raydium (pool AMM indexé) — voir la limite
        # documentée dans backtest_token_pathaware(). Utile pour les analyses
        # de wallets dont l'historique inclut d'anciens tokens déjà migrés.
        pathaware_result = await backtest_token_pathaware(token_mint, purchase_block_time, tp_pct=tp_pct, sl_pct=sl_pct)
        if pathaware_result is not None:
            backtest_result = pathaware_result
        # PRIORITÉ 3 — repli final : prix d'entrée réel (on-chain) vs prix
        # ACTUEL (instantané, pas de chemin — peut rater un TP touché en
        # cours de route si le token a dumpé depuis).
        elif entry_price_sol and pair_data:
            try:
                current_price_native = float(pair_data.get("priceNative") or 0)
                if current_price_native > 0:
                    real_result_pct = ((current_price_native - entry_price_sol) / entry_price_sol) * 100
                    effective_tp = tp_pct if tp_pct is not None else config.TP_PCT
                    effective_sl = sl_pct if sl_pct is not None else config.SL_PCT

                    if real_result_pct >= effective_tp:
                        backtest_result = {"token_mint": token_mint, "result_pct": effective_tp, "hit_tp": True,
                                            "hit_sl": False, "reason": f"TP atteint (+{effective_tp}%) [calcul réel depuis le prix d'entrée, instantané]"}
                    elif effective_sl and real_result_pct <= -effective_sl:
                        backtest_result = {"token_mint": token_mint, "result_pct": -effective_sl, "hit_tp": False,
                                            "hit_sl": True, "reason": f"SL touché (-{effective_sl}%) [calcul réel depuis le prix d'entrée, instantané]"}
                    else:
                        backtest_result = {"token_mint": token_mint, "result_pct": real_result_pct, "hit_tp": False,
                                            "hit_sl": False, "reason": "Ni TP ni SL — position encore ouverte [calcul réel depuis le prix d'entrée, instantané]"}
            except (TypeError, ValueError, ZeroDivisionError):
                pass

    if not pair_data:
        return {
            **backtest_result,
            "symbol": None, "name": None, "price_usd": None,
            "market_cap": None, "liquidity_usd": None, "volume_24h": None,
            "purchase_date": _format_timestamp(purchase_block_time),
            "dexscreener_url": None,
            "entry_market_cap_usd": None, "entry_price_sol": entry_price_sol,
        }

    base_token = pair_data.get("baseToken", {})
    return {
        **backtest_result,
        "symbol": base_token.get("symbol"),
        "name": base_token.get("name"),
        "price_usd": pair_data.get("priceUsd"),
        "market_cap": pair_data.get("marketCap") or pair_data.get("fdv"),
        "liquidity_usd": pair_data.get("liquidity", {}).get("usd"),
        "volume_24h": pair_data.get("volume", {}).get("h24"),
        "price_change_24h": pair_data.get("priceChange", {}).get("h24"),
        "purchase_date": _format_timestamp(purchase_block_time),
        "dexscreener_url": pair_data.get("url"),
        "entry_market_cap_usd": entry_market_cap_usd,
        "entry_price_sol": entry_price_sol,
    }


def _format_timestamp(block_time) -> str:
    """Formate un blockTime Unix en date lisible, ou 'date inconnue' si absent."""
    if not block_time:
        return "date inconnue"
    import time as time_module
    return time_module.strftime("%Y-%m-%d %H:%M", time_module.localtime(block_time))


async def get_token_age_minutes(token_mint: str) -> float:
    """
    Retourne l'âge du token en minutes depuis sa création, via le champ
    pairCreatedAt de DexScreener (timestamp en millisecondes). Retourne
    None si l'info n'est pas disponible (le filtre Max Token Age qui
    l'utilise doit alors laisser passer plutôt que rejeter à tort).
    """
    import time
    pair_data = await _get_pair_data(token_mint)
    created_at_ms = pair_data.get("pairCreatedAt")
    if not created_at_ms:
        return None
    age_seconds = time.time() - (created_at_ms / 1000)
    return age_seconds / 60
