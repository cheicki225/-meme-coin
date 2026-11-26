"""Profile the Helius SQLite inbox with synthetic events in a temporary directory.

This command does not load dotenv, open a network client, read wallet data, or
start the bot. Its output describes local storage latency and bytes only.
"""
from __future__ import annotations

import argparse
import gc
import json
import math
import os
import platform
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_EVENTS = 1_000
MAX_EVENTS = 5_000
STREAM = "mission21:synthetic-profile"


def _percentile(samples: list[float], fraction: float) -> float:
    """Return a linearly interpolated percentile for a non-empty sample."""
    if not samples:
        raise ValueError("percentile requires at least one sample")
    if not 0 <= fraction <= 1:
        raise ValueError("fraction must be between zero and one")
    ordered = sorted(samples)
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summarize_latency_ms(samples: list[float]) -> dict[str, int | float]:
    """Summarize measured operation latency without claiming a service SLO."""
    if not samples:
        raise ValueError("latency summary requires at least one sample")
    if any(not math.isfinite(sample) or sample < 0 for sample in samples):
        raise ValueError("latency samples must be finite and non-negative")
    return {
        "samples": len(samples),
        "p50_ms": round(_percentile(samples, 0.50), 3),
        "p95_ms": round(_percentile(samples, 0.95), 3),
        "p99_ms": round(_percentile(samples, 0.99), 3),
        "max_ms": round(max(samples), 3),
    }


def _phase(samples: list[float], elapsed_seconds: float, operations: int) -> dict:
    return {
        **summarize_latency_ms(samples),
        "elapsed_seconds": round(elapsed_seconds, 6),
        "operations_per_second": round(operations / elapsed_seconds, 2)
        if elapsed_seconds > 0
        else 0.0,
    }


def run_profile(event_count: int, isolated_directory: Path) -> dict:
    """Run insert, duplicate, claim, and acknowledgement phases on a temp DB."""
    if type(event_count) is not int or not 10 <= event_count <= MAX_EVENTS:
        raise ValueError(f"event_count must be an integer between 10 and {MAX_EVENTS}")

    directory = Path(isolated_directory)
    directory.mkdir(parents=True, exist_ok=True)
    database_path = directory / "mission21-inbox.sqlite3"
    metrics_path = directory / "mission21-metrics.jsonl"
    if database_path.exists() or metrics_path.exists():
        raise FileExistsError("profile output files must not already exist")

    # All persistence and telemetry is constrained to this new temporary folder.
    with patch.dict(
        os.environ,
        {
            "TRADING_MODE": "PAPER",
            "LIVE_EXECUTION_ENABLED": "false",
            "PYTHON_DOTENV_DISABLED": "true",
            "EXECUTION_METRICS_PATH": str(metrics_path),
        },
    ):
        import execution_metrics
        from helius_event_store import HeliusEventStore

        execution_metrics._QUEUE.join()
        store = HeliusEventStore(
            database_path,
            max_pending_events=event_count + 1,
            max_pending_bytes=8 * 1024 * 1024,
            lease_seconds=60,
        )
        baseline_time = time.time()
        signatures = [f"synthetic-signature-{index:08d}" for index in range(event_count)]
        payloads = [
            {"benchmark": "synthetic", "sequence": index, "padding": "x" * 64}
            for index in range(event_count)
        ]
        payload_bytes_each = len(
            json.dumps(
                payloads[0],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
        )

        insert_samples: list[float] = []
        inserted_ids: list[int] = []
        phase_started = time.perf_counter()
        started = phase_started
        for index, (signature, payload) in enumerate(zip(signatures, payloads)):
            result = store.enqueue(
                STREAM,
                signature,
                payload,
                slot=50_000 + index,
                transaction_index=index,
                is_replay=True,
                now=baseline_time,
            )
            insert_samples.append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            if not result.inserted:
                raise RuntimeError("synthetic unique insert was unexpectedly deduplicated")
            inserted_ids.append(result.event_id)
        insert_elapsed = time.perf_counter() - phase_started

        duplicate_samples: list[float] = []
        phase_started = time.perf_counter()
        started = phase_started
        for index, (signature, payload) in enumerate(zip(signatures, payloads)):
            result = store.enqueue(
                STREAM,
                signature,
                payload,
                slot=50_000 + index,
                transaction_index=index,
                is_replay=True,
                now=baseline_time + 1,
            )
            duplicate_samples.append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            if result.inserted or result.event_id != inserted_ids[index]:
                raise RuntimeError("duplicate did not reuse the original inbox row")
        duplicate_elapsed = time.perf_counter() - phase_started

        claim_samples: list[float] = []
        acknowledge_samples: list[float] = []
        phase_started = time.perf_counter()
        started = phase_started
        for expected_signature in signatures:
            event = store.claim_next(STREAM, now=baseline_time + 2)
            claim_samples.append((time.perf_counter() - started) * 1000)
            if event is None or event.signature != expected_signature:
                raise RuntimeError("inbox claim order did not match synthetic canonical order")
            started = time.perf_counter()
            store.mark_processed(
                event.event_id,
                claim_generation=event.claim_generation,
                recovery=event.recovery_claim,
                recovery_generation=event.recovery_generation,
                now=baseline_time + 3,
            )
            acknowledge_samples.append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
        drain_elapsed = time.perf_counter() - phase_started

        # Drain telemetry writes before the temporary directory can be removed.
        execution_metrics._QUEUE.join()

        database = sqlite3.connect(database_path)
        try:
            integrity = database.execute("PRAGMA integrity_check").fetchone()[0]
            schema_version = database.execute("PRAGMA user_version").fetchone()[0]
            row_count = database.execute("SELECT COUNT(*) FROM event_inbox").fetchone()[0]
        finally:
            database.close()
        aggregate = store.metrics()
        checkpoint_sequence, _checkpoint_signature = store.checkpoint(STREAM)
        if integrity != "ok" or row_count != event_count:
            raise RuntimeError("synthetic inbox integrity or row-count check failed")
        if aggregate["processed"] != event_count or aggregate["pending"] != 0:
            raise RuntimeError("synthetic inbox did not drain to the expected state")
        if aggregate["duplicates"] != event_count or checkpoint_sequence != event_count:
            raise RuntimeError("synthetic dedupe or checkpoint count did not match")

        component_sizes = {
            path.name: path.stat().st_size
            for path in (
                database_path,
                Path(f"{database_path}-wal"),
                Path(f"{database_path}-shm"),
            )
            if path.exists()
        }
        storage_bytes = sum(component_sizes.values())
        total_elapsed = insert_elapsed + duplicate_elapsed + drain_elapsed
        return {
            "status": "offline_synthetic_component_profile",
            "source_commit": _source_commit(),
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "sqlite": sqlite3.sqlite_version,
                "trading_mode": "PAPER",
                "live_execution_enabled": False,
                "network_calls": 0,
            },
            "workload": {
                "events": event_count,
                "payload_bytes_each": payload_bytes_each,
                "unique_rows_inserted": int(aggregate["received"]),
                "duplicate_observations": int(aggregate["duplicates"]),
                "processed_rows": int(aggregate["processed"]),
                "pending_rows_after_drain": int(aggregate["pending"]),
                "unknown_rows_after_drain": int(aggregate["unknown"]),
                "checkpoint_sequence": checkpoint_sequence,
                "integrity_check": integrity,
                "schema_version": schema_version,
            },
            "phases": {
                "unique_enqueue": _phase(insert_samples, insert_elapsed, event_count),
                "duplicate_enqueue": _phase(duplicate_samples, duplicate_elapsed, event_count),
                "claim_and_acknowledge": {
                    "elapsed_seconds": round(drain_elapsed, 6),
                    "claim": summarize_latency_ms(claim_samples),
                    "acknowledge": summarize_latency_ms(acknowledge_samples),
                    "events_per_second": round(event_count / drain_elapsed, 2)
                    if drain_elapsed > 0
                    else 0.0,
                },
                "total_profile_elapsed_seconds": round(total_elapsed, 6),
            },
            "storage": {
                "temporary_database_components_bytes": component_sizes,
                "total_bytes": storage_bytes,
                "bytes_per_unique_row": round(storage_bytes / event_count, 2),
                "telemetry_file_bytes": metrics_path.stat().st_size
                if metrics_path.exists()
                else 0,
                "provider_or_chain_fees": "not measured",
            },
        }


def _source_commit() -> str | None:
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=DEFAULT_EVENTS)
    arguments = parser.parse_args()
    if not 10 <= arguments.events <= MAX_EVENTS:
        parser.error(f"--events must be between 10 and {MAX_EVENTS}")
    with tempfile.TemporaryDirectory(prefix="mission21-sqlite-profile-") as directory:
        result = run_profile(arguments.events, Path(directory))
        gc.collect()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
