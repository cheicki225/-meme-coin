"""
════════════════════════════════════════════════════════════════
RPC CLIENT — Appels RPC Solana avec secours lecture seule
════════════════════════════════════════════════════════════════
Centralise les appels JSON-RPC vers Solana. Le point d'accès HTTP primaire
provient de SOLANA_RPC_URL, de Helius si sa clé est configurée, ou du RPC
public Solana par défaut. Alchemy est un secours facultatif pour les lectures.

Remplace les appels aiohttp directs à config.HELIUS_RPC_URL dispersés
dans fund_tracer.py, pattern_detector.py, paper_trader.py.
"""

import asyncio
import hashlib
import json
import logging
import math
import random
import time
from collections import defaultdict, deque
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import aiohttp
from aiohttp.resolver import ThreadedResolver

import config
from execution_metrics import measure_execution

log = logging.getLogger("rpc_client")

_RPC_METRICS = {
    "requests": 0,
    "errors": 0,
    "fallbacks": 0,
    "rate_limited": 0,
    "coalesced": 0,
    "errors_by_method": defaultdict(int),
    "latencies_ms": deque(maxlen=200),
}

_MAX_INLINE_RETRY_AFTER_SECONDS = 30.0
_MAX_INFLIGHT_READS = 256
_RETRYABLE_READ_METHODS = frozenset({
    "getAccountInfo", "getBalance", "getBlock", "getBlockHeight", "getBlockProduction",
    "getBlockTime", "getBlocks", "getBlocksWithLimit", "getClusterNodes", "getEpochInfo",
    "getEpochSchedule", "getFeeForMessage", "getFirstAvailableBlock", "getGenesisHash",
    "getHealth", "getHighestSnapshotSlot", "getIdentity", "getInflationGovernor",
    "getInflationRate", "getLargestAccounts", "getLatestBlockhash", "getLeaderSchedule",
    "getMaxRetransmitSlot", "getMaxShredInsertSlot", "getMinimumBalanceForRentExemption",
    "getMultipleAccounts", "getProgramAccounts", "getRecentBlockhash",
    "getRecentPerformanceSamples", "getRecentPrioritizationFees", "getSignatureStatuses",
    "getSignaturesForAddress", "getSlot", "getSlotLeader", "getStakeActivation",
    "getStakeMinimumDelegation", "getSupply", "getTokenAccountBalance",
    "getTokenAccountsByDelegate", "getTokenAccountsByOwner", "getTokenLargestAccounts",
    "getTokenSupply", "getTransaction", "getTransactionCount", "getTransfersByAddress",
    "getVersion", "getVoteAccounts", "getTipAccounts", "isBlockhashValid",
    "simulateTransaction",
})
_RATE_LIMIT_ERROR_WORDS = ("rate limit", "rate-limit", "too many requests", "throttl", "quota")
_RATE_LIMITERS: dict[str, "_RateLimiter"] = {}
_INFLIGHT_READS: dict[str, asyncio.Task] = {}


def get_metrics() -> dict:
    """Return a copy of aggregate RPC health data without endpoint URLs or keys."""
    return {
        "requests": _RPC_METRICS["requests"],
        "errors": _RPC_METRICS["errors"],
        "fallbacks": _RPC_METRICS["fallbacks"],
        "rate_limited": _RPC_METRICS["rate_limited"],
        "coalesced": _RPC_METRICS["coalesced"],
        "errors_by_method": dict(_RPC_METRICS["errors_by_method"]),
        "latencies_ms": list(_RPC_METRICS["latencies_ms"]),
    }


def reset_metrics() -> None:
    _RPC_METRICS["requests"] = 0
    _RPC_METRICS["errors"] = 0
    _RPC_METRICS["fallbacks"] = 0
    _RPC_METRICS["rate_limited"] = 0
    _RPC_METRICS["coalesced"] = 0
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


class RpcSubmissionOutcomeUnknown(RuntimeError):
    """A transaction submission may have reached the network and needs reconciliation."""

    def __init__(self, signature: str):
        self.signature = signature
        super().__init__(
            "sendTransaction outcome is unknown; read-only reconciliation is required"
        )


class _RateLimiter:
    """Apply a per-provider request spacing and shared 429 cooldown."""

    def __init__(self, max_per_second: float):
        self._interval_seconds = 1.0 / max_per_second
        self._next_request_at = 0.0
        self._cooldown_until = 0.0
        self._lock = asyncio.Lock()

    async def wait_if_needed(self) -> bool:
        """Reserve one request slot, or report that the provider is cooling down."""
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            if now < self._cooldown_until:
                return False
            wait_time = max(0.0, self._next_request_at - now)
            if wait_time:
                await asyncio.sleep(wait_time)
            now = loop.time()
            if now < self._cooldown_until:
                return False
            self._next_request_at = max(now, self._next_request_at) + self._interval_seconds
            return True

    def defer(self, seconds: float) -> None:
        """Pause this provider after a rate-limit response without exposing its URL."""
        if not math.isfinite(seconds) or seconds <= 0:
            return
        now = asyncio.get_running_loop().time()
        self._cooldown_until = max(self._cooldown_until, now + seconds)


def _provider_key(url: str) -> str:
    """Return a credential-free rate-limit key derived only from the host."""
    try:
        return (urlsplit(url).hostname or "unknown").lower().rstrip(".")
    except ValueError:
        return "unknown"


def _limiter_for_url(url: str) -> _RateLimiter:
    key = _provider_key(url)
    limiter = _RATE_LIMITERS.get(key)
    if limiter is None:
        requests_per_second = (
            config.HELIUS_ENHANCED_REQUESTS_PER_SECOND
            if key == "api.helius.xyz"
            else config.SOLANA_RPC_REQUESTS_PER_SECOND
        )
        limiter = _RateLimiter(requests_per_second)
        _RATE_LIMITERS[key] = limiter
    return limiter


async def wait_for_provider_slot(url: str) -> bool:
    """Wait for a slot on the provider serving ``url``; never return the URL."""
    return await _limiter_for_url(url).wait_if_needed()


def defer_provider(url: str, seconds: float) -> None:
    """Apply an in-memory cooldown to a provider host."""
    _limiter_for_url(url).defer(seconds)


def note_rate_limited_response(url: str, retry_after: str | None) -> float:
    """Record a 429 and defer that provider while keeping its URL private."""
    delay = _retry_delay(0, retry_after)
    _RPC_METRICS["rate_limited"] += 1
    defer_provider(url, delay)
    return delay


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


def _inflight_read_key(payload: dict, timeout: int) -> str | None:
    """Hash an explicitly allowlisted in-flight read without retaining its inputs."""
    method = payload.get("method")
    if not isinstance(method, str) or method not in _RETRYABLE_READ_METHODS:
        return None
    canonical = {key: value for key, value in payload.items() if key != "id"}
    try:
        encoded = json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    digest = hashlib.sha256(encoded).hexdigest()
    loop_id = id(asyncio.get_running_loop())
    return f"{loop_id}:{_provider_key(config.HELIUS_RPC_URL)}:{timeout}:{digest}"


def _remove_inflight_read(key: str, task: asyncio.Task) -> None:
    if _INFLIGHT_READS.get(key) is task:
        _INFLIGHT_READS.pop(key, None)


async def rpc_post(payload: dict, timeout: int = 12) -> dict:
    """Call RPC, coalescing only identical concurrent read requests."""
    if payload.get("method") == "sendTransaction":
        raise ValueError(
            "sendTransaction requires submit_transaction and its transaction signature"
        )

    key = _inflight_read_key(payload, timeout)
    if key is None:
        return await _rpc_post_uncached(payload, timeout)

    task = _INFLIGHT_READS.get(key)
    if task is not None and task.done():
        _remove_inflight_read(key, task)
        task = None
    if task is None:
        if len(_INFLIGHT_READS) >= _MAX_INFLIGHT_READS:
            return await _rpc_post_uncached(payload, timeout)
        task = asyncio.create_task(_rpc_post_uncached(payload, timeout))
        _INFLIGHT_READS[key] = task
        task.add_done_callback(lambda completed: _remove_inflight_read(key, completed))
    else:
        _RPC_METRICS["coalesced"] += 1
        with measure_execution("rpc.read_coalesced", "internal"):
            return await asyncio.shield(task)
    return await asyncio.shield(task)


async def _rpc_post_uncached(payload: dict, timeout: int) -> dict:
    """Call one read or submission request, using failover only for reads."""
    with measure_execution("rpc.request", "solana_rpc") as metric:
        method = str(payload.get("method", "unknown"))
        can_retry_read = method in _RETRYABLE_READ_METHODS
        started = time.monotonic()
        result = await _try_endpoint(
            config.HELIUS_RPC_URL,
            payload,
            timeout,
            max_retries=3 if can_retry_read else 1,
            provider="rpc_primary",
        )
        used_fallback = False
        if result is None and config.ALCHEMY_RPC_URL and can_retry_read:
            used_fallback = True
            log.debug("Primary RPC failed for %s; trying configured fallback", method)
            result = await _try_endpoint(
                config.ALCHEMY_RPC_URL, payload, timeout, provider="rpc_fallback"
            )

        metric["provider"] = "rpc_fallback" if used_fallback else "rpc_primary"

        ok = result is not None
        _record_request(method, ok=ok, elapsed_ms=(time.monotonic() - started) * 1000, fallback=used_fallback)
        if ok:
            return result
        metric["outcome"] = "failure"
        log.warning(
            "RPC request failed for method %s (%s)",
            method,
            "primary only" if not can_retry_read else "primary + fallback" if config.ALCHEMY_RPC_URL else "primary",
        )
        return {}


async def submit_transaction(payload: dict, signature: str, timeout: int = 12) -> str:
    """Submit a signed transaction once to the primary RPC, preserving its signature.

    Submission outcomes are not retried or failed over. A missing, malformed, or
    different response remains uncertain until the signature is reconciled with
    read-only status queries by the operator.
    """
    if payload.get("method") != "sendTransaction":
        raise ValueError("submit_transaction only accepts sendTransaction payloads")
    if not isinstance(signature, str) or not signature.strip():
        raise ValueError("A non-empty transaction signature is required for submission")

    method = "sendTransaction"
    started = time.monotonic()
    with measure_execution("rpc.request", "solana_rpc") as metric:
        result = await _try_endpoint(
            config.HELIUS_RPC_URL,
            payload,
            timeout,
            max_retries=1,
            provider="rpc_primary",
        )
        ok = result == signature
        _record_request(
            method,
            ok=ok,
            elapsed_ms=(time.monotonic() - started) * 1000,
            fallback=False,
        )
        metric["provider"] = "rpc_primary"
        if ok:
            return signature

        metric["outcome"] = "failure"
        log.warning(
            "RPC sendTransaction outcome is unknown; read-only reconciliation is required"
        )
        raise RpcSubmissionOutcomeUnknown(signature)


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
    url = config.HELIUS_PARSE_TX_URL
    try:
        with measure_execution("rpc.rate_limit_wait", "helius") as limit_metric:
            if not await wait_for_provider_slot(url):
                limit_metric["outcome"] = "skipped"
                return {}
        async with aiohttp.ClientSession(connector=get_http_connector(), connector_owner=False) as session:
            async with session.post(
                url,
                json={"transactions": [signature]},
                timeout=aiohttp.ClientTimeout(total=10),
                allow_redirects=False,
            ) as resp:
                if resp.status == 429:
                    note_rate_limited_response(url, resp.headers.get("Retry-After"))
                    log.warning("Enhanced transaction provider returned HTTP 429")
                    return {}
                if resp.status != 200:
                    return {}
                data = await resp.json()
                return data[0] if data else {}
    except Exception as e:
        # Provider URLs can contain API keys; never log exception text or URLs.
        log.debug("Parsed transaction fetch failed (%s)", type(e).__name__)
        return {}


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse Retry-After seconds or HTTP date without exposing response data."""
    if not value:
        return None
    try:
        requested = float(value)
    except (TypeError, ValueError):
        try:
            requested = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return None
    if math.isfinite(requested) and requested >= 0:
        return requested
    return None


def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
    """Use Retry-After exactly, adding only positive jitter; otherwise back off."""
    requested = _retry_after_seconds(retry_after)
    if requested is not None:
        return requested + random.uniform(0, 0.25)
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


def _rate_limited_json_rpc_error(error) -> bool:
    """Recognize explicit JSON-RPC quota errors without assuming all -32005 are 429s."""
    if not isinstance(error, dict):
        return False
    message = error.get("message", "")
    return isinstance(message, str) and any(
        word in message.lower() for word in _RATE_LIMIT_ERROR_WORDS
    )


async def _try_endpoint(
    url: str,
    payload: dict,
    timeout: int,
    max_retries: int = 3,
    *,
    provider: str = "unknown",
):
    """Call one RPC endpoint with bounded retries for transient failures.

    Provider response bodies and exception messages are never logged: either
    may contain credential-bearing URLs or request data.
    """
    if not url:
        return None

    method = str(payload.get("method", "?"))
    if method not in _RETRYABLE_READ_METHODS:
        max_retries = 1
    limiter = _limiter_for_url(url)
    for attempt in range(max_retries):
        retry_after = None
        retry_delay = None
        with measure_execution("rpc.endpoint_attempt", provider) as attempt_metric:
            with measure_execution("rpc.rate_limit_wait", provider) as limit_metric:
                if not await limiter.wait_if_needed():
                    limit_metric["outcome"] = "skipped"
                    attempt_metric["outcome"] = "skipped"
                    return None
            try:
                async with aiohttp.ClientSession(
                    connector=get_http_connector(), connector_owner=False
                ) as session:
                    async with session.post(
                        url,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                        allow_redirects=False,
                    ) as resp:
                        if resp.status == 429 or resp.status >= 500:
                            log.warning(
                                "RPC %s returned HTTP %s (attempt %s/%s)",
                                method, resp.status, attempt + 1, max_retries,
                            )
                            if resp.status == 429:
                                retry_after = resp.headers.get("Retry-After")
                                _RPC_METRICS["rate_limited"] += 1
                                retry_delay = _retry_delay(attempt, retry_after)
                                limiter.defer(retry_delay)
                                if retry_delay > _MAX_INLINE_RETRY_AFTER_SECONDS:
                                    log.warning(
                                        "RPC %s cooldown exceeds inline retry window; using fallback if configured",
                                        method,
                                    )
                                    attempt_metric["outcome"] = "failure"
                                    return None
                        elif resp.status != 200:
                            log.warning("RPC %s returned non-retryable HTTP %s", method, resp.status)
                            attempt_metric["outcome"] = "failure"
                            return None
                        else:
                            try:
                                data = await resp.json()
                            except (aiohttp.ContentTypeError, ValueError):
                                log.warning("RPC %s returned invalid JSON", method)
                            else:
                                if not isinstance(data, dict):
                                    log.warning("RPC %s returned an invalid JSON-RPC envelope", method)
                                    attempt_metric["outcome"] = "failure"
                                    return None
                                if "error" not in data:
                                    attempt_metric["outcome"] = "success"
                                    return data.get("result")
                                error = data["error"]
                                code = error.get("code") if isinstance(error, dict) else None
                                log.warning("RPC %s returned JSON-RPC error code %s", method, code)
                                if _rate_limited_json_rpc_error(error):
                                    retry_delay = _retry_delay(attempt)
                                    _RPC_METRICS["rate_limited"] += 1
                                    limiter.defer(retry_delay)
                                if not _retryable_json_rpc_error(error):
                                    attempt_metric["outcome"] = "failure"
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
                attempt_metric["outcome"] = "failure"
                return None
            attempt_metric["outcome"] = "failure"

        if attempt + 1 < max_retries:
            delay = retry_delay if retry_delay is not None else _retry_delay(attempt)
            await asyncio.sleep(delay)

    return None
