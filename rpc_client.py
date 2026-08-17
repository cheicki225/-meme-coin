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


async def _try_endpoint(url: str, payload: dict, timeout: int, max_retries: int = 3):
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

    CORRIGÉ (3e fois) suite à une vague d'échecs "Internal error" (-32603)
    et de timeouts observés en conditions réelles : cette fonction n'avait
    AUCUN nouvel essai — chaque échec, même transitoire (un simple hoquet
    passager côté Helius sous forte charge), était traité comme définitif et
    abandonné immédiatement, causant des trous de données évitables ("données
    indisponibles" en cascade). Réessaie maintenant jusqu'à 2 fois de plus
    (max_retries=3 au total), avec un court délai croissant entre chaque
    tentative — mais UNIQUEMENT pour les erreurs qui ont une chance réelle
    d'être temporaires (timeout, erreur serveur -32603/HTTP 5xx) ; les
    erreurs de paramètres invalides (ex: -32602) ne sont PAS réessayées,
    puisque réessayer ne changerait rien à un mauvais paramètre.
    """
    if not url:
        return None

    method = payload.get("method", "?")
    last_error_was_transient = True

    for attempt in range(max_retries):
        await _rate_limiter.wait_if_needed()
        try:
            async with aiohttp.ClientSession(connector=get_http_connector(), connector_owner=False) as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status >= 500:
                        body = await resp.text()
                        print(f"⚠️  RPC '{method}' -> statut HTTP {resp.status} (essai {attempt + 1}/{max_retries}) : {body[:200]}")
                        last_error_was_transient = True
                    elif resp.status != 200:
                        body = await resp.text()
                        print(f"⚠️  RPC '{method}' -> statut HTTP {resp.status} : {body[:300]}")
                        return None  # erreur non-serveur (4xx) — pas la peine de réessayer
                    else:
                        data = await resp.json()
                        if "error" in data:
                            error_code = data["error"].get("code") if isinstance(data["error"], dict) else None
                            # -32603 (Internal error) et "timeout" dans le message sont
                            # traités comme transitoires ; le reste (ex: -32602 Invalid
                            # param) est définitif, pas la peine de réessayer.
                            is_transient = error_code == -32603 or "timeout" in str(data["error"]).lower()
                            print(f"⚠️  RPC '{method}' -> erreur JSON-RPC (essai {attempt + 1}/{max_retries}) : {data['error']}")
                            if not is_transient:
                                return None
                            last_error_was_transient = True
                        else:
                            return data.get("result")
        except asyncio.TimeoutError:
            print(f"⚠️  RPC '{method}' -> timeout après {timeout}s (essai {attempt + 1}/{max_retries})")
            last_error_was_transient = True
        except Exception as e:
            print(f"⚠️  RPC '{method}' -> exception : {type(e).__name__}: {e}")
            return None  # exception inattendue — pas la peine de réessayer à l'aveugle

        if attempt < max_retries - 1:
            await asyncio.sleep(0.5 * (attempt + 1))  # 0.5s, puis 1s

    return None
