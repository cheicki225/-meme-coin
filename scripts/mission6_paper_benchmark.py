"""Run an isolated, synthetic PAPER component soak and latency benchmark.

No external network, wallet, private key, Jupiter endpoint, or Solana transaction
is used. The two RPC endpoints bind to loopback and answer only getHealth.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import platform
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web


ROOT = Path(__file__).resolve().parents[1]


def _percentiles(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    if not ordered:
        return {"p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0}

    def percentile(fraction: float) -> float:
        position = (len(ordered) - 1) * fraction
        low, high = math.floor(position), math.ceil(position)
        value = ordered[low] if low == high else ordered[low] + (ordered[high] - ordered[low]) * (position - low)
        return round(value, 3)

    return {"p50_ms": percentile(0.5), "p95_ms": percentile(0.95), "p99_ms": percentile(0.99)}


def _git_value(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


async def _serve(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets  # aiohttp exposes no public ephemeral-port accessor
    return runner, f"http://127.0.0.1:{sockets[0].getsockname()[1]}"


async def _run(cycles: int, output: Path) -> dict:
    if cycles < 100:
        raise ValueError("Use at least 100 cycles for a meaningful synthetic soak")

    with tempfile.TemporaryDirectory(prefix="mission6-paper-soak-") as isolated_dir:
        root = Path(isolated_dir)
        os.environ.update({
            "TRADING_MODE": "PAPER",
            "PYTHON_DOTENV_DISABLED": "true",
            "LIVE_EXECUTION_ENABLED": "false",
            "ALLOW_MANUAL_TRANSFERS": "false",
            "HELIUS_API_KEY": "",
            "ALCHEMY_API_KEY": "",
            "GOPLUS_API_KEY": "",
            "GOPLUS_API_SECRET": "",
            "MOBULA_API_KEY": "",
            "GMGN_API_KEY": "",
            "SOLSCAN_API_KEY": "",
            "ANTHROPIC_API_KEY": "",
            "GROK_API_KEY": "",
            "SOLANA_PRIVATE_KEY": "",
            "WALLET_ENCRYPTION_KEY": "",
            "JUPITER_API_KEY": "",
            "BOT_TOKEN": "",
            "TELEGRAM_BOT_TOKEN": "",
            "WALLET_PRIVATE_KEY": "",
            "DATA_BACKEND": "sqlite",
            "SQLITE_DB_FILE": str(root / "state.sqlite3"),
            "DATA_FILE": str(root / "legacy-state.json"),
            "WALLET_ACTIVITY_DB_FILE": str(root / "wallet-activity.sqlite3"),
            "EXECUTION_METRICS_PATH": str(root / "metrics.jsonl"),
        })
        sys.path.insert(0, str(ROOT))

        import config
        import paper_trader
        import rpc_client
        from execution_metrics import summarize_metrics
        from monitoring_list import DataStore
        from rpc_client import _RateLimiter
        from websocket_listener import NewTokenListener

        if config.TRADING_MODE != "PAPER" or config.LIVE_EXECUTION_ENABLED:
            raise RuntimeError("Benchmark safety configuration did not remain PAPER-only")

        logging.getLogger("rpc_client").disabled = True

        primary_attempts: dict[str, int] = {}
        fallback_attempts: dict[str, int] = {}
        counters = {"primary_429": 0, "primary_503": 0, "fallback_503": 0}

        async def primary_rpc(request: web.Request) -> web.Response:
            payload = await request.json()
            request_id = int(payload.get("id", 0))
            key = str(request_id)
            primary_attempts[key] = primary_attempts.get(key, 0) + 1
            if request_id % 997 == 0:
                counters["primary_503"] += 1
                return web.Response(status=503)
            if request_id % 101 == 0:
                counters["primary_503"] += 1
                return web.Response(status=503)
            if request_id % 53 == 0 and primary_attempts[key] == 1:
                counters["primary_429"] += 1
                return web.Response(status=429, headers={"Retry-After": "0"})
            if request_id % 100 == 0:
                await asyncio.sleep(0.002)
            return web.json_response({"jsonrpc": "2.0", "id": request_id, "result": {"ok": True}})

        async def fallback_rpc(request: web.Request) -> web.Response:
            payload = await request.json()
            request_id = int(payload.get("id", 0))
            if request_id % 997 == 0:
                counters["fallback_503"] += 1
                return web.Response(status=503)
            fallback_attempts[str(request_id)] = fallback_attempts.get(str(request_id), 0) + 1
            if request_id % 100 == 0:
                await asyncio.sleep(0.004)
            return web.json_response({"jsonrpc": "2.0", "id": request_id, "result": {"ok": True}})

        primary_app, fallback_app = web.Application(), web.Application()
        primary_app.router.add_post("/", primary_rpc)
        fallback_app.router.add_post("/", fallback_rpc)
        primary_runner, primary_url = await _serve(primary_app)
        fallback_runner, fallback_url = await _serve(fallback_app)

        try:
            config.HELIUS_RPC_URL = primary_url
            config.ALCHEMY_RPC_URL = fallback_url
            rpc_client._rate_limiter = _RateLimiter(max_per_second=100_000)
            rpc_client._retry_delay = lambda *_args, **_kwargs: 0.0
            rpc_client.reset_metrics()
            primary_endpoint_ms: list[float] = []
            fallback_endpoint_ms: list[float] = []
            original_measure = rpc_client.measure_execution

            @contextmanager
            def capture_measure(stage: str, provider: str = "unknown", **kwargs):
                started = time.perf_counter()
                try:
                    with original_measure(stage, provider, **kwargs) as metric:
                        yield metric
                finally:
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    if stage == "rpc.endpoint_attempt":
                        (fallback_endpoint_ms if provider == "rpc_fallback" else primary_endpoint_ms).append(elapsed_ms)

            rpc_client.measure_execution = capture_measure

            detected = 0
            detection_ms: list[float] = []

            async def on_new_token(_token: dict) -> None:
                nonlocal detected
                detected += 1

            listener = NewTokenListener(on_new_token)

            async def fake_parsed(_signature: str) -> dict:
                if int(_signature) % 200 == 0:
                    await asyncio.sleep(0.001)
                return {
                    "events": {"pump_fun": {"mint": f"synthetic-mint-{_signature}", "creator": "synthetic-dev"}},
                    "slot": int(_signature),
                }

            listener._fetch_parsed_transaction = fake_parsed
            paper_trader.get_sol_usd_rate = lambda: asyncio.sleep(0, result=100.0)
            trader = paper_trader.PaperTrader(data_store=None)
            settings = {"buy_amount_sol": 0.1, "buy_mode": "simple"}
            rpc_ms: list[float] = []
            trade_primitive_ms: list[float] = []
            cycle_failures = 0
            simulated_entry_fees_sol = 0.0
            simulated_exit_fees_sol = 0.0

            store = DataStore()
            store.state["mission6_soak"] = {
                "mode": "PAPER", "cycles": 0, "rpc_success": 0,
                "rpc_failures": 0, "detected": 0,
            }
            store.save()
            restart_ms: list[float] = []
            started_run = time.perf_counter()

            for index in range(1, cycles + 1):
                cycle_started = time.perf_counter()
                response = await rpc_client.rpc_post({"jsonrpc": "2.0", "id": index, "method": "getHealth"}, timeout=2)
                rpc_ms.append((time.perf_counter() - cycle_started) * 1000)
                if not response:
                    cycle_failures += 1

                detection_started = time.perf_counter()
                await listener._handle_creation(str(index))
                detection_ms.append((time.perf_counter() - detection_started) * 1000)

                trade_started = time.perf_counter()
                buy = await trader._execute_buy(f"synthetic-mint-{index}", 1.0, settings)
                entry = await paper_trader._paper_entry_accounting(
                    buy["cost_basis_usd"], settings["buy_amount_sol"], 0.000005,
                    sol_usd_rate=buy["sol_usd_rate"],
                )
                sell = await trader._execute_sell(
                    f"synthetic-mint-{index}", buy["units"], 1.1, settings,
                    simulated_fee_sol=0.000005, sol_usd_rate=100.0,
                )
                simulated_entry_fees_sol += entry["simulated_fees_sol"]
                simulated_exit_fees_sol += sell["simulated_fee_sol"]
                trade_primitive_ms.append((time.perf_counter() - trade_started) * 1000)

                state = store.state["mission6_soak"]
                state["cycles"] = index
                state["rpc_success"] += bool(response)
                state["rpc_failures"] += not bool(response)
                state["detected"] = detected

                if index % 250 == 0 or index == cycles:
                    store.save()
                if index % 500 == 0 and index != cycles:
                    restart_started = time.perf_counter()
                    store = DataStore()
                    restart_ms.append((time.perf_counter() - restart_started) * 1000)
                    recovered = store.state.get("mission6_soak", {})
                    if recovered.get("cycles") != index or recovered.get("detected") != detected:
                        raise RuntimeError(f"SQLite restart recovery mismatch after cycle {index}")

            elapsed_s = time.perf_counter() - started_run
            metric_summary = summarize_metrics(str(root / "metrics.jsonl"))
            state = store.state.get("mission6_soak", {})
            result = {
                "benchmark": "mission6_synthetic_paper_component_soak",
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "code_revision": _git_value("rev-parse", "HEAD"),
                "platform": platform.platform(),
                "python": platform.python_version(),
                "mode": config.TRADING_MODE,
                "live_execution_enabled": config.LIVE_EXECUTION_ENABLED,
                "external_hosts_contacted": [],
                "transaction_submissions": 0,
                "data": {"kind": "synthetic_only", "real_token_or_ohlcv_data": False, "authenticated_provider_provenance": False},
                "workload": {
                    "cycles": cycles,
                    "virtual_duration_hours": round(24 * cycles / 10_000, 2),
                    "wall_duration_seconds": round(elapsed_s, 3),
                    "rpc_methods": ["getHealth"],
                    "rpc_fault_injection": {**counters, "fallback_used": len(fallback_attempts), "final_failures": cycle_failures},
                    "sqlite_restarts_verified": len(restart_ms),
                    "crash_recovery_process_test": "covered by tests/test_datastore_recovery.py",
                    "detection_events": detected,
                    "state_cycles_recovered": state.get("cycles"),
                },
                "latency": {
                    "rpc_read_end_to_end": {"count": len(rpc_ms), **_percentiles(rpc_ms)},
                    "rpc_primary_attempt": {"count": len(primary_endpoint_ms), **_percentiles(primary_endpoint_ms)},
                    "rpc_fallback_attempt": {"count": len(fallback_endpoint_ms), **_percentiles(fallback_endpoint_ms)},
                    "token_detection_dispatch": {"count": len(detection_ms), **_percentiles(detection_ms)},
                    "paper_trade_primitives": {"count": len(trade_primitive_ms), **_percentiles(trade_primitive_ms)},
                    "sqlite_restart_recovery": {"count": len(restart_ms), **_percentiles(restart_ms)},
                },
                "paper_fee_simulation": {
                    "entry_fee_per_cycle_sol": 0.000005,
                    "exit_fee_per_cycle_sol": 0.000005,
                    "entry_fees_total_sol": round(simulated_entry_fees_sol, 9),
                    "exit_fees_total_sol": round(simulated_exit_fees_sol, 9),
                    "usd_conversion_rate": 100.0,
                    "market_execution_or_actual_chain_fees": False,
                },
                "telemetry": metric_summary,
                "limitations": [
                    "Accelerated synthetic component test; not a 24-hour wall-clock deployment soak.",
                    "Loopback timings do not represent Solana or provider latency.",
                    "No full bot startup, WebSocket provider, Jupiter, DEX program, Telegram, or real token feed was used.",
                    "Fault injection covers HTTP 429/503, fallback, SQLite close/reopen, and a separate subprocess-crash test; it does not emulate physical disk failure.",
                    "No actual SOL price, chain transaction fee, slippage, MEV, or historical OHLCV fee reconstruction is measured.",
                ],
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return result
        finally:
            connector = getattr(rpc_client, "_connector", None)
            if connector is not None and not connector.closed:
                await connector.close()
                rpc_client._connector = None
            await primary_runner.cleanup()
            await fallback_runner.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=10_000)
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "docs" / "benchmarks" / "mission6-paper-soak.json",
    )
    args = parser.parse_args()
    result = asyncio.run(_run(args.cycles, args.output.resolve()))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
