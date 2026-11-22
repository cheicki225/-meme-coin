"""
════════════════════════════════════════════════════════════════
RPC CLIENT — Appels RPC Solana avec fallback automatique
════════════════════════════════════════════════════════════════
Centralise les appels JSON-RPC vers Solana. Le point d'accès HTTP primaire
provient de SOLANA_RPC_URL, de Helius si sa clé est configurée, ou du RPC
public Solana par défaut. Alchemy est un secours facultatif.

Remplace les appels aiohttp directs à config.HELIUS_RPC_URL dispersés
dans fund_tracer.py, pattern_detector.py, paper_trader.py.
"""

import asyncio
import logging
import math
import random
import time
from email.utils import parsedate_to_datetime
from collections import defaultdict, deque
import aiohttp
from aiohttp.resolver import ThreadedResolver

import config
from execution_metrics import measure_execution

log = logging.getLogger("rpc_client")

_RPC_METRICS = {
    "requests": 0,
    "errors": 0,
    "fallbacks": 0,
    "errors_by_method": defaultdict(int),
    "latencies_ms": deque(maxlen=200),
}


def get_metrics() -> dict:
    """Return a copy of aggregate RPC health data without endpoint URLs or keys."""
    return {
        "requests": _RPC_METRICS["requests"],
        "errors": _RPC_METRICS["errors"],
        "fallbacks": _RPC_METRICS["fallbacks"],
        "errors_by_method": dict(_RPC_METRICS["errors_by_method"]),
        "latencies_ms": list(_RPC_METRICS["latencies_ms"]),
    }


def reset_metrics() -> None:
    _RPC_METRICS["requests"] = 0
    _RPC_METRICS["errors"] = 0
    _RPC_METRICS["fallbacks"] = 0
    _RPC_METRICS["errors_by_method"].clear()
    _RPC_METRICS["latencies_ms"].clear()


def _record_request(method: str, *, ok: bool, elapsed_ms: float, fallback: bool) -> None:
    _RPC_METRICS["requests"] += 1
    _RPC_METRICS["latencies_ms"].append(round(elapsed_ms, 2))
    if fallback:
        _RPC_METRICS["fallbacks"] += 1
    if not ok:
        _RPC_METRICS["errors"] += 1
        _RPC_METRICS["errors_by_method"][method] += 1


class RpcUnavailableError(RuntimeError):
    pass


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
    """Try the configured primary HTTP RPC then optional Alchemy fallback."""
    with measure_execution("rpc.request", "solana_rpc") as metric:
        method = str(payload.get("method", "unknown"))
        started = time.monotonic()
        result = await _try_endpoint(config.HELIUS_RPC_URL, payload, timeout)
        used_fallback = False
        if result is None and config.ALCHEMY_RPC_URL:
            used_fallback = True
            log.debug("Primary RPC failed for %s; trying configured fallback", method)
            result = await _try_endpoint(config.ALCHEMY_RPC_URL, payload, timeout)

        metric["provider"] = "rpc_fallback" if used_fallback else "rpc_primary"

        ok = result is not None
        _record_request(method, ok=ok, elapsed_ms=(time.monotonic() - started) * 1000, fallback=used_fallback)
        if ok:
            return result
        metric["outcome"] = "failure"
        log.warning(
            "RPC request failed for method %s (primary%s)",
            method, " + fallback" if config.ALCHEMY_RPC_URL else "",
        )
        return {}


async def rpc_post_strict(payload: dict, timeout: int = 12):
    """Return RPC data or raise instead of silently treating failure as zero."""
    result = await rpc_post(payload, timeout=timeout)
    if result == {}:
        raise RpcUnavailableError(f"RPC request failed for method {payload.get('method', 'unknown')}")
    return result


async def fetch_parsed_transaction(signature: str) -> dict:
    """
    EXTRAIT de copytrade_listener.py (était _fetch_parsed_transaction, méthode
    privée) pour être partagé avec d'autres modules qui ont besoin de la même
    API Enhanced Transactions Helius — voir protection_scanner.py, qui
    l'utilise maintenant pour sa propre détection en WebSocket (logsSubscribe)
    au lieu du sondage RPC répété qu'il faisait avant.
    """
    if not config.HELIUS_API_KEY:
        return {}
    try:
        await _rate_limiter.wait_if_needed()
        async with aiohttp.ClientSession(connector=get_http_connector(), connector_owner=False) as session:
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
        # Provider URLs can contain API keys; never log exception text or URLs.
        log.debug("Parsed transaction fetch failed (%s)", type(e).__name__)
        return {}


def _retry_delay(attempt: int, retry_after: str = None) -> float:
    """Exponential backoff with jitter; honor Retry-After without leaking data."""
    if retry_after:
        try:
            requested = float(retry_after)
        except (TypeError, ValueError):
            try:
                requested = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                requested = -1
        if math.isfinite(requested) and requested >= 0:
            return min(requested + random.uniform(0, 0.25), 30.0)
    base = min(0.5 * (2 ** max(attempt, 0)), 8.0)
    return base + random.uniform(0, min(base * 0.25, 1.0))


def _retryable_json_rpc_error(error) -> bool:
    """Retry only transient server/rate-limit JSON-RPC errors."""
    if not isinstance(error, dict):
        return False
    code = error.get("code")
    message = error.get("message", "")
    return code in (-32603, -32005) or (
        isinstance(message, str) and any(
            word in message.lower()
            for word in ("timeout", "rate limit", "temporarily unavailable")
        )
    )


async def _try_endpoint(url: str, payload: dict, timeout: int, max_retries: int = 3):
    """Call one RPC endpoint with bounded retries for transient failures.

    Provider response bodies and exception messages are never logged: either
    may contain credential-bearing URLs or request data.
    """
    if not url:
        return None

    method = str(payload.get("method", "?"))
    for attempt in range(max_retries):
        await _rate_limiter.wait_if_needed()
        retry_after = None
        try:
            async with aiohttp.ClientSession(
                connector=get_http_connector(), connector_owner=False
            ) as session:
                async with session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        log.warning(
                            "RPC %s returned HTTP %s (attempt %s/%s)",
                            method, resp.status, attempt + 1, max_retries,
                        )
                        if resp.status == 429:
                            retry_after = resp.headers.get("Retry-After")
                    elif resp.status != 200:
                        log.warning("RPC %s returned non-retryable HTTP %s", method, resp.status)
                        return None
                    else:
                        try:
                            data = await resp.json()
                        except (aiohttp.ContentTypeError, ValueError):
                            log.warning("RPC %s returned invalid JSON", method)
                        else:
                            if not isinstance(data, dict):
                                log.warning("RPC %s returned an invalid JSON-RPC envelope", method)
                                return None
                            if "error" not in data:
                                return data.get("result")
                            error = data["error"]
                            code = error.get("code") if isinstance(error, dict) else None
                            log.warning("RPC %s returned JSON-RPC error code %s", method, code)
                            if not _retryable_json_rpc_error(error):
                                return None
        except asyncio.TimeoutError:
            log.warning("RPC %s timed out (attempt %s/%s)", method, attempt + 1, max_retries)
        except aiohttp.ClientError as exc:
            log.warning(
                "RPC %s transport error %s (attempt %s/%s)",
                method, type(exc).__name__, attempt + 1, max_retries,
            )
        except Exception as exc:
            log.warning("RPC %s failed with %s", method, type(exc).__name__)
            return None

        if attempt + 1 < max_retries:
            await asyncio.sleep(_retry_delay(attempt, retry_after))

    return None
