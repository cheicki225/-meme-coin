"""Privacy-conscious, dependency-free execution telemetry.

Records stage durations and outcomes only. Never pass wallet addresses, private
keys, token balances, transaction signatures, or raw provider responses here.
Telemetry is best-effort: a metrics write must never interrupt a trade path.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_LOCK = threading.Lock()
_ALLOWED_OUTCOMES = {"success", "failure", "unknown", "skipped"}


def _number(value: object, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"Invalid {label}") from None
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"Invalid {label}")
    return result


def _label(value: object, label: str, limit: int = 48) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit or any(ord(c) < 32 for c in text):
        raise ValueError(f"Invalid {label}")
    return text


def record_execution_metric(
    stage: str,
    duration_ms: float,
    outcome: str = "success",
    provider: str = "unknown",
    *,
    path: str | os.PathLike[str] | None = None,
) -> bool:
    """Append one sanitized event. Returns False rather than raising on I/O errors."""
    try:
        event = {
            "ts": time.time(),
            "stage": _label(stage, "stage"),
            "duration_ms": round(_number(duration_ms, "duration"), 3),
            "outcome": _label(outcome, "outcome"),
            "provider": _label(provider, "provider"),
        }
        if event["outcome"] not in _ALLOWED_OUTCOMES:
            raise ValueError("Invalid outcome")
        target = Path(path or os.getenv("EXECUTION_METRICS_PATH", "data/execution_metrics.jsonl"))
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(event, separators=(",", ":"), allow_nan=False) + "\n"
        with _LOCK:
            with target.open("a", encoding="utf-8") as stream:
                stream.write(encoded)
        return True
    except (OSError, ValueError, TypeError):
        return False


@contextmanager
def measure_execution(
    stage: str, provider: str = "unknown", *, path: str | os.PathLike[str] | None = None
) -> Iterator[dict[str, str]]:
    """Measure a stage and record success/failure/unknown without masking errors."""
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
            state["outcome"], provider, path=path,
        )


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


def summarize_metrics(path: str | os.PathLike[str]) -> dict:
    """Summarize JSONL telemetry. Invalid/truncated lines are ignored."""
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                    stage = _label(item.get("stage"), "stage")
                    outcome = _label(item.get("outcome"), "outcome")
                    provider = _label(item.get("provider", "unknown"), "provider")
                    duration = _number(item.get("duration_ms"), "duration")
                    if outcome not in _ALLOWED_OUTCOMES:
                        continue
                    grouped[(stage, provider)].append({"duration_ms": duration, "outcome": outcome})
                except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
                    continue
    except OSError:
        return {"events": 0, "stages": {}}

    stages = {}
    total = 0
    for (stage, provider), events in sorted(grouped.items()):
        durations = [e["duration_ms"] for e in events]
        count = len(events)
        failures = sum(e["outcome"] in {"failure", "unknown"} for e in events)
        key = f"{stage}:{provider}"
        stages[key] = {
            "count": count,
            "failure_rate": round(failures / count, 4),
            "p50_ms": _percentile(durations, 0.50),
            "p95_ms": _percentile(durations, 0.95),
            "p99_ms": _percentile(durations, 0.99),
            "max_ms": round(max(durations), 3),
        }
        total += count
    return {"events": total, "stages": stages}
