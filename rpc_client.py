"""
════════════════════════════════════════════════════════════════
RPC CLIENT — Appels RPC Solana avec fallback automatique
════════════════════════════════════════════════════════════════
Centralise tous les appels JSON-RPC vers Solana. Essaie Helius en
premier ; si la requête échoue (timeout, erreur HTTP, rate limit)
et qu'une clé Alchemy est configurée, retente automatiquement dessus.

Remplace les appels aiohttp directs à config.HELIUS_RPC_URL dispersés
dans fund_tracer.py, pattern_detector.py, paper_trader.py.
"""

import asyncio
import logging
import aiohttp
from aiohttp.resolver import ThreadedResolver

import config

log = logging.getLogger("rpc_client")


class _RateLimiter:
    """
    Limiteur de débit à fenêtre glissante — corrige des 429 persistants
    malgré plusieurs tentatives de cadencement LOCAL (pause dans une seule
    fonction). Le vrai problème : chaque partie du script (recherche du
    créateur, du financeur, vérification étoile/neutre...) consommait sa
    propre part de quota sans savoir ce que les autres avaient déjà utilisé.
    Ce limiteur est GLOBAL — tous les appels Helius du script, peu importe
    d'où ils viennent, passent par la même fenêtre de 10 req/s (plan Gratuit
    Helius, confirmé via dashboard), avec une marge de sécurité à 8.
    """
    def __init__(self, max_per_second: float = 5.0):
        self.max_per_second = max_per_second
        self._timestamps = []
        self._lock = asyncio.Lock()

    async def wait_if_needed(self):
        async with self._lock:
            now = asyncio.get_event_loop().time()
            # Garde seulement les requêtes de la dernière seconde
            self._timestamps = [t for t in self._timestamps if now - t < 1.0]

            if len(self._timestamps) >= self.max_per_second:
                oldest = self._timestamps[0]
                wait_time = 1.0 - (now - oldest)
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                now = asyncio.get_event_loop().time()
                self._timestamps = [t for t in self._timestamps if now - t < 1.0]

            self._timestamps.append(now)


_rate_limiter = _RateLimiter(max_per_second=5.0)  # entre 3 (stable mais lent) et 6 (encore quelques 429) — test intermédiaire


# ── Fix DNS Windows ────────────────────────────────────────────
# aiodns (résolveur DNS par défaut d'aiohttp) échoue silencieusement sur
# certaines configurations Windows ("Could not contact DNS servers") même
# quand la connexion internet fonctionne normalement — bug connu lié à
# c-ares, souvent déclenché par un pare-feu/VPN/antivirus qui bloque le
# type de requête DNS utilisé par aiodns. ThreadedResolver utilise à la
# place le résolveur DNS natif du système, qui fonctionne dans ce cas.
_connector = None


def get_http_connector():
    """
    Connecteur aiohttp partagé, avec fix DNS Windows (voir commentaire ci-dessus).
    Réutilisable par tout module qui fait des appels aiohttp externes
    (DexScreener, Jupiter, GoPlus, Claude/Grok...).
    """
    global _connector
    if _connector is None or _connector.closed:
        _connector = aiohttp.TCPConnector(resolver=ThreadedResolver())
    return _connector


async def rpc_post(payload: dict, timeout: int = 12) -> dict:
    """
    Envoie une requête JSON-RPC à Helius, avec fallback sur Alchemy si
    configuré et si Helius échoue. Retourne le champ "result" de la
    réponse (ou {} en cas d'échec total).
    """
    result = await _try_endpoint(config.HELIUS_RPC_URL, payload, timeout)
    if result is not None:
        return result

    if config.ALCHEMY_RPC_URL:
        log.debug("Helius a échoué, tentative sur Alchemy...")
        result = await _try_endpoint(config.ALCHEMY_RPC_URL, payload, timeout)
        if result is not None:
            return result

    log.warning(f"Échec RPC (Helius{' + Alchemy' if config.ALCHEMY_RPC_URL else ''}) pour méthode {payload.get('method')}")
    return {}


async def _try_endpoint(url: str, payload: dict, timeout: int):
    """
    CORRIGÉ suite à un vrai échec répété et non diagnosticable de
    getTransfersByAddress : les erreurs étaient loggées en niveau "debug"
    (invisible dans la sortie normale du bot), obligeant à écrire un script
    de diagnostic séparé à chaque nouvelle panne pour voir la vraie cause.
    Affiche maintenant le détail complet directement, sans setup supplémentaire.

    CORRIGÉ (2e fois) : applique maintenant le limiteur de débit GLOBAL
    (_rate_limiter) avant CHAQUE appel — corrige des 429 persistants qui
    survenaient même avec un cadencement local dans une seule fonction,
    parce que d'autres parties du script consommaient du quota en parallèle
    sans coordination entre elles.
    """
    if not url:
        return None

    await _rate_limiter.wait_if_needed()

    method = payload.get("method", "?")
    try:
        async with aiohttp.ClientSession(connector=get_http_connector(), connector_owner=False) as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    print(f"⚠️  RPC '{method}' -> statut HTTP {resp.status} : {body[:300]}")
                    return None
                data = await resp.json()
                if "error" in data:
                    print(f"⚠️  RPC '{method}' -> erreur JSON-RPC : {data['error']}")
                    return None
                return data.get("result")
    except asyncio.TimeoutError:
        print(f"⚠️  RPC '{method}' -> timeout après {timeout}s")
        return None
    except Exception as e:
        print(f"⚠️  RPC '{method}' -> exception : {type(e).__name__}: {e}")
        return None
