"""Privacy-conscious, best-effort execution telemetry.

Only fixed low-cardinality stage/provider labels and durations are recorded.
The trade path enqueues to a bounded in-memory queue; a daemon writer handles
disk I/O so a slow filesystem cannot stall transaction preparation or landing.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Iterator


_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}\Z")
_ALLOWED_OUTCOMES = {"success", "failure", "unknown", "skipped"}
_ALLOWED_STAGES = frozenset({
    "websocket.copytrade_message", "copytrade.process_transaction",
    "jupiter.order", "jupiter.build", "jupiter.submit", "jupiter.accounting_fetch",
    "jupiter.confirm", "jupiter.execute_swap", "jupiter.sign",
    "jupiter.cpi_simulate",
    "decision.new_token_dispatch", "decision.copytrade_buy", "decision.copytrade_sell",
    "decision.evaluate_dev", "strategy.open_position", "strategy.partial_close",
    "strategy.close_position", "rpc.request", "rpc.endpoint_attempt",
    "rpc.rate_limit_wait", "rpc.read_coalesced", "websocket.new_token_creation",
    "websocket.parse_transaction", "websocket.creation_callback",
    "websocket.reconnect_wait", "websocket.subscription_send",
})
_ALLOWED_PROVIDERS = frozenset({
    "unknown", "helius", "jupiter", "solana_rpc", "local", "internal",
    "rpc_primary", "rpc_fallback", "solana_ws",
})
_MAX_EVENTS = 10_000
_MAX_QUEUE = 4_096
_MAX_FILE_BYTES = 5 * 1024 * 1024
_EVENTS: deque[dict] = deque(maxlen=_MAX_EVENTS)
_EVENT_LOCK = threading.Lock()
_QUEUE: Queue[tuple[Path, dict] | None] = Queue(maxsize=_MAX_QUEUE)
_WORKER_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None
_DROPPED_EVENTS = 0


def _number(value: object, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"Invalid {label}") from None
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"Invalid {label}")
    return result


def _label(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not _LABEL.fullmatch(text):
        raise ValueError(f"Invalid {label}")
    if label == "stage" and text not in _ALLOWED_STAGES:
        raise ValueError("Invalid stage")
    if label == "provider" and text not in _ALLOWED_PROVIDERS:
        raise ValueError("Invalid provider")
    return text


def _metrics_path(path: str | os.PathLike[str] | None) -> Path:
    return Path(path or os.getenv("EXECUTION_METRICS_PATH", "data/execution_metrics.jsonl"))


def _write_worker() -> None:
    while True:
        item = _QUEUE.get()
        try:
            if item is None:
                return
            path, event = item
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists() and path.stat().st_size >= _MAX_FILE_BYTES:
                    rotated = path.with_suffix(path.suffix + ".1")
                    try:
                        rotated.unlink(missing_ok=True)
                        os.replace(path, rotated)
                    except OSError:
                        # Metrics are best-effort; retain the live trade path if
                        # disk rotation is unavailable.
                        pass
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(event, separators=(",", ":"), allow_nan=False) + "\n")
            except (OSError, TypeError, ValueError):
                global _DROPPED_EVENTS
                with _EVENT_LOCK:
                    _DROPPED_EVENTS += 1
        finally:
            _QUEUE.task_done()


def _ensure_worker() -> None:
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(
                target=_write_worker, name="execution-metrics-writer", daemon=True,
            )
            _WORKER.start()


def record_execution_metric(
    stage: str,
    duration_ms: float,
    outcome: str = "success",
    provider: str = "unknown",
    *,
    path: str | os.PathLike[str] | None = None,
) -> bool:
    """Record a sanitized event without allowing telemetry to raise in a trade path."""
    global _DROPPED_EVENTS
    try:
        event = {
            "ts": round(time.time(), 3),
            "stage": _label(stage, "stage"),
            "duration_ms": round(_number(duration_ms, "duration"), 3),
            "outcome": _label(outcome, "outcome"),
            "provider": _label(provider, "provider"),
        }
        if event["outcome"] not in _ALLOWED_OUTCOMES:
            raise ValueError("Invalid outcome")
        with _EVENT_LOCK:
            _EVENTS.append(event)
        _ensure_worker()
        _QUEUE.put_nowait((_metrics_path(path), event))
        return True
    except Full:
        with _EVENT_LOCK:
            _DROPPED_EVENTS += 1
        return False
    except (OSError, ValueError, TypeError):
        return False
    except Exception:
        # Telemetry must never interrupt a trade or recovery path.
        return False


@contextmanager
def measure_execution(
    stage: str,
    provider: str = "unknown",
    *,
    path: str | os.PathLike[str] | None = None,
) -> Iterator[dict[str, str]]:
    """Measure a block, preserving its result or exception."""
    state = {"outcome": "success"}
    started = time.perf_counter()
    try:
        yield state
    except BaseException:
        state["outcome"] = "unknown"
        raise
    finally:
        record_execution_metric(
            stage, (time.perf_counter() - started) * 1000,
            state["outcome"], state.get("provider", provider), path=path,
        )


def measure_async(stage: str, provider: str = "unknown"):
    """Decorate an async function with best-effort duration and outcome capture."""
    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            with measure_execution(stage, provider):
                return await function(*args, **kwargs)
        return wrapped
    return decorate


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = (len(ordered) - 1) * percentile
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return round(ordered[low], 3)
    fraction = rank - low
    return round(ordered[low] + (ordered[high] - ordered[low]) * fraction, 3)


def _read_events(path: Path) -> list[dict]:
    events: deque[dict] = deque(maxlen=_MAX_EVENTS)
    for source in (path.with_suffix(path.suffix + ".1"), path):
        try:
            with source.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                        event = {
                            "stage": _label(item.get("stage"), "stage"),
                            "provider": _label(item.get("provider", "unknown"), "provider"),
                            "outcome": _label(item.get("outcome"), "outcome"),
                            "duration_ms": _number(item.get("duration_ms"), "duration"),
                        }
                        if event["outcome"] in _ALLOWED_OUTCOMES:
                            events.append(event)
                    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
                        continue
        except OSError:
            continue
    return list(events)


def summarize_metrics(path: str | os.PathLike[str] | None = None) -> dict:
    """Return bounded p50/p95/p99 and failure-rate summaries for each stage."""
    _QUEUE.join()
    events = _read_events(_metrics_path(path)) if path is not None or os.getenv("EXECUTION_METRICS_PATH") else None
    if events is None:
        with _EVENT_LOCK:
            events = [dict(item) for item in _EVENTS]

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for event in events:
        grouped[(event["stage"], event["provider"])].append(event)

    stages = {}
    for (stage, provider), items in sorted(grouped.items()):
        durations = [item["duration_ms"] for item in items]
        count = len(items)
        failures = sum(item["outcome"] in {"failure", "unknown"} for item in items)
        stages[f"{stage}:{provider}"] = {
            "count": count,
            "failure_rate": round(failures / count, 4),
            "p50_ms": _percentile(durations, 0.50),
            "p95_ms": _percentile(durations, 0.95),
            "p99_ms": _percentile(durations, 0.99),
            "max_ms": round(max(durations), 3),
        }
    return {"events": len(events), "dropped_events": _DROPPED_EVENTS, "stages": stages}


def format_metrics_report(max_rows: int = 8) -> str:
    """Format a small, non-sensitive summary suitable for the private Telegram bot."""
    summary = summarize_metrics(_metrics_path(None))
    if not summary["events"]:
        summary = summarize_metrics()
    if not summary["stages"]:
        return "Aucune mesure d'exécution enregistrée dans ce processus."
    rows = sorted(
        summary["stages"].items(),
        key=lambda item: (item[1]["p95_ms"], item[1]["count"]),
        reverse=True,
    )[:max_rows]
    lines = [f"Mesures : {summary['events']} | événements perdus : {summary['dropped_events']}"]
    for key, item in rows:
        lines.append(
            f"{key} — n={item['count']}, p50={item['p50_ms']:.1f} ms, "
            f"p95={item['p95_ms']:.1f} ms, p99={item['p99_ms']:.1f} ms, "
            f"échecs={item['failure_rate']:.1%}"
        )
    return "\n".join(lines)
