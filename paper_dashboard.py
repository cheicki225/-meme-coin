"""Loopback-only, read-only dashboard for persisted PAPER state.

The dashboard never loads the bot configuration or dotenv files. Its data
sources are explicit local paths and every value is an aggregate with a source,
timestamp, and status. Runtime-only health is intentionally reported UNKNOWN.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


_SQLITE_TIMEOUT_SECONDS = 0.2
_MAX_STATE_BYTES = 8 * 1024 * 1024
_MAX_METRICS_FILE_BYTES = 2 * 1024 * 1024
_MAX_METRIC_EVENTS = 5_000
_FEE_ACCOUNTING_VERSION = 1
_METRIC_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}\Z")
_ALLOWED_OUTCOMES = {"success", "failure", "unknown", "skipped"}
_ALLOWED_STAGES = frozenset({
    "websocket.copytrade_message", "copytrade.process_transaction",
    "jupiter.order", "jupiter.build", "jupiter.submit", "jupiter.accounting_fetch",
    "jupiter.confirm", "jupiter.execute_swap", "jupiter.sign", "jupiter.cpi_simulate",
    "decision.new_token_dispatch", "decision.copytrade_buy", "decision.copytrade_sell",
    "decision.evaluate_dev", "strategy.open_position", "strategy.partial_close",
    "strategy.close_position", "rpc.request", "rpc.endpoint_attempt",
    "rpc.rate_limit_wait", "rpc.read_coalesced", "websocket.new_token_creation",
    "websocket.parse_transaction", "websocket.creation_callback", "websocket.reconnect_wait",
    "websocket.subscription_send", "event.inbox_enqueue", "event.parse", "event.decision",
    "event.processing", "event.retry", "event.unknown", "event.end_to_end",
})
_ALLOWED_PROVIDERS = frozenset({
    "unknown", "helius", "jupiter", "solana_rpc", "local", "internal",
    "rpc_primary", "rpc_fallback", "solana_ws",
})


_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'">
  <title>PAPER snapshot viewer</title>
  <style>
    :root { color-scheme: dark; font: 15px system-ui, sans-serif; background: #0d1520; color: #e7eef7; }
    body { margin: 0 auto; max-width: 1180px; padding: 28px 20px; }
    h1 { margin: 0 0 8px; font-size: 1.8rem; }
    .intro, .foot { color: #a8b8cb; }
    .notice { margin: 18px 0; padding: 12px 14px; border: 1px solid #38516c; border-radius: 10px; background: #142438; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
    article { min-height: 116px; padding: 15px; border: 1px solid #2b3d51; border-radius: 10px; background: #121e2c; }
    h2 { margin: 0 0 14px; font-size: 1rem; color: #a8c5e3; }
    .value { overflow-wrap: anywhere; font-size: 1.1rem; font-weight: 650; }
    .meta { margin-top: 12px; color: #9baabd; font-size: .78rem; overflow-wrap: anywhere; }
    .foot { margin-top: 22px; font-size: .8rem; }
  </style>
</head>
<body>
  <h1>PAPER snapshot viewer</h1>
  <div class="intro">Local, read-only view of persisted snapshots. No trading controls.</div>
  <div class="notice">Unknown runtime values stay marked UNKNOWN; this page does not infer that stored trades were PAPER. It does not contact RPC, Helius, Telegram, a wallet, or an execution service.</div>
  <main class="grid" id="cards" aria-live="polite"></main>
  <div class="foot" id="generated"></div>
  <script>
    const fields = [
      ["runtime_mode", "Runtime mode"], ["runtime_heartbeat", "Bot process"],
      ["emergency_stop", "Emergency stop"], ["signals", "Signals"],
      ["open_positions", "Open positions in stored state"], ["exposure_cost_basis_usd", "Open cost basis (USD)"],
      ["pnl_recorded_usd", "Recorded total PnL (USD)"], ["unrealized_pnl_usd", "Unrealized PnL (USD)"],
      ["fee_history", "Recorded fees"], ["helius_inbox", "Helius event inbox"],
      ["helius_event_freshness", "Latest Helius event timestamps"],
      ["helius_checkpoints", "Helius checkpoints and recovery"], ["helius_provider_health", "Helius provider health"],
      ["rpc_health", "RPC health"], ["telegram_delivery", "Telegram delivery"],
      ["performance", "Execution performance"], ["observed_errors", "Observed telemetry failures"],
    ];
    const cards = document.getElementById("cards");
    for (const [id, label] of fields) {
      const card = document.createElement("article");
      const title = document.createElement("h2");
      const value = document.createElement("div");
      const meta = document.createElement("div");
      title.textContent = label;
      value.className = "value";
      meta.className = "meta";
      card.append(title, value, meta);
      cards.append(card);
      card.dataset.point = id;
    }
    function display(value) {
      if (value === null || value === undefined) return "UNKNOWN";
      if (typeof value === "object") return JSON.stringify(value);
      if (typeof value === "number") return Number.isFinite(value) ? String(value) : "UNKNOWN";
      if (typeof value === "boolean") return value ? "ON" : "OFF";
      return String(value);
    }
    async function refresh() {
      try {
        const response = await fetch("/api/status", { cache: "no-store", credentials: "same-origin" });
        if (!response.ok) throw new Error("unavailable");
        const snapshot = await response.json();
        for (const card of cards.children) {
          const point = snapshot[card.dataset.point] || { status: "UNKNOWN", source: "not available" };
          card.children[1].textContent = display(point.value);
          card.children[2].textContent = `${point.status} · ${point.source} · as of ${point.as_of || "UNKNOWN"}${point.detail ? ` · ${point.detail}` : ""}`;
        }
        document.getElementById("generated").textContent = `Snapshot generated at ${snapshot.generated_at_utc || "UNKNOWN"}`;
      } catch (_) {
        for (const card of cards.children) {
          card.children[1].textContent = "UNKNOWN";
          card.children[2].textContent = "UNKNOWN · local snapshot unavailable";
        }
        document.getElementById("generated").textContent = "Snapshot generated at UNKNOWN";
      }
    }
    refresh();
    window.setInterval(refresh, 5000);
  </script>
</body>
</html>
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _point(
    status: str,
    value: Any,
    source: str,
    as_of: str | None,
    detail: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "value": value,
        "source": source,
        "as_of": as_of,
        "detail": detail,
    }


def _unknown(source: str, detail: str, as_of: str | None = None) -> dict[str, Any]:
    return _point("UNKNOWN", None, source, as_of, detail)


def _reject_json_constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _as_of(path: Path, *, include_wal: bool = False) -> str | None:
    candidates = [path]
    if include_wal:
        candidates.append(path.with_name(path.name + "-wal"))
    timestamps = []
    for candidate in candidates:
        try:
            timestamps.append(candidate.stat().st_mtime)
        except OSError:
            continue
    if not timestamps:
        return None
    return datetime.fromtimestamp(max(timestamps), timezone.utc).isoformat(timespec="seconds")


def _epoch_as_utc(value: Any) -> str | None:
    epoch = _finite_number(value)
    if epoch is None or epoch < 0:
        return None
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _readonly_connection(path: Path) -> sqlite3.Connection:
    resolved = path.resolve(strict=True)
    connection = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro",
        uri=True,
        timeout=_SQLITE_TIMEOUT_SECONDS,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute(f"PRAGMA busy_timeout = {int(_SQLITE_TIMEOUT_SECONDS * 1000)}")
    return connection


def _read_state(path: Path | None) -> tuple[dict[str, Any] | None, str | None, str]:
    if path is None:
        return None, None, "state source not configured"
    try:
        if not path.is_file():
            return None, None, "state source unavailable"
        as_of = _as_of(path, include_wal=path.suffix.lower() not in {".json"})
        if path.suffix.lower() == ".json":
            size = path.stat().st_size
            if size > _MAX_STATE_BYTES:
                return None, as_of, "state snapshot exceeds local read limit"
            with path.open("r", encoding="utf-8") as stream:
                state = json.load(
                    stream,
                    parse_constant=_reject_json_constant,
                    object_pairs_hook=_reject_duplicate_keys,
                )
        else:
            with closing(_readonly_connection(path)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if "bot_state" not in tables:
                    return None, as_of, "state schema unavailable"
                row = connection.execute(
                    "SELECT payload FROM bot_state WHERE singleton = 1"
                ).fetchone()
                if row is None:
                    return None, as_of, "persisted state unavailable"
                payload = row["payload"]
                if not isinstance(payload, str) or len(payload.encode("utf-8")) > _MAX_STATE_BYTES:
                    return None, as_of, "state snapshot is invalid"
                state = json.loads(
                    payload,
                    parse_constant=_reject_json_constant,
                    object_pairs_hook=_reject_duplicate_keys,
                )
        if not isinstance(state, dict):
            return None, as_of, "state snapshot is invalid"
        return state, as_of, "persisted state observed"
    except (OSError, sqlite3.Error, json.JSONDecodeError, UnicodeError, TypeError, ValueError):
        return None, _as_of(path, include_wal=True), "state read failed"


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def _safe_sum(values: list[float | None]) -> float | None:
    if any(value is None or value < 0 for value in values):
        return None
    try:
        result = math.fsum(value for value in values if value is not None)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def _position_list(state: dict[str, Any], key: str) -> list[dict[str, Any]] | None:
    value = state.get(key)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        return None
    return value


def _fee_history_is_complete(state: dict[str, Any]) -> bool:
    if (
        state.get("pnl_fee_history_incomplete") is not False
        or type(state.get("pnl_fee_accounting_version")) is not int
        or state["pnl_fee_accounting_version"] != _FEE_ACCOUNTING_VERSION
    ):
        return False
    open_rows = _position_list(state, "open_positions")
    closed_rows = _position_list(state, "closed_positions")
    trade_rows = _position_list(state, "trades")
    if open_rows is None or closed_rows is None or trade_rows is None:
        return False
    positions = [*open_rows, *closed_rows]
    pnl_trades = [trade for trade in trade_rows if "pnl_usd" in trade or "fees_usd" in trade]
    if pnl_trades and not positions:
        return False
    for position in positions:
        fee = _finite_number(position.get("simulated_fees_usd"))
        if (
            fee is None or fee < 0
            or type(position.get("fee_accounting_version")) is not int
            or position["fee_accounting_version"] != _FEE_ACCOUNTING_VERSION
            or position.get("fee_accounting_complete") is not True
        ):
            return False
    for trade in trade_rows:
        if "pnl_usd" not in trade and "fees_usd" not in trade:
            continue
        if (
            type(trade.get("fee_accounting_version")) is not int
            or trade["fee_accounting_version"] != _FEE_ACCOUNTING_VERSION
            or trade.get("fee_accounting_complete") is not True
        ):
            return False
    return True


def _state_points(state: dict[str, Any] | None, as_of: str | None, detail: str) -> dict[str, dict[str, Any]]:
    if state is None:
        return {
            "emergency_stop": _unknown("persisted state", detail, as_of),
            "open_positions": _unknown("persisted state", detail, as_of),
            "exposure_cost_basis_usd": _unknown("persisted state", detail, as_of),
            "pnl_recorded_usd": _unknown("persisted state", detail, as_of),
            "unrealized_pnl_usd": _unknown("persisted state", "no current price feed is persisted", as_of),
            "fee_history": _unknown("persisted state", detail, as_of),
        }

    stop = state.get("emergency_stop")
    stop_point = (
        _point("OBSERVED", stop, "persisted emergency-stop flag", as_of)
        if type(stop) is bool
        else _unknown("persisted emergency-stop flag", "stored flag is missing or malformed")
    )

    open_positions = _position_list(state, "open_positions")
    if open_positions is None:
        count_point = _unknown("persisted state", "open-position list is missing or malformed", as_of)
        exposure_point = _unknown("persisted state", "open-position cost basis is unavailable", as_of)
    else:
        count_point = _point(
            "OBSERVED", len(open_positions), "persisted open-position count", as_of,
            "stored data does not identify the runtime mode",
        )
        bases = [_finite_number(item.get("cost_basis_usd")) for item in open_positions]
        exposure = _safe_sum(bases)
        if exposure is not None:
            exposure_point = _point(
                "OBSERVED",
                round(exposure, 6),
                "sum of persisted cost_basis_usd; not a mark-to-market value",
                as_of,
                "stored data does not identify the runtime mode",
            )
        else:
            exposure_point = _unknown("persisted state", "one or more open cost-basis values are unavailable", as_of)

    aggregate_pnl_complete = _fee_history_is_complete(state)
    total_pnl = _finite_number(state.get("total_pnl_usd"))
    pnl_point = (
        _point(
            "OBSERVED" if aggregate_pnl_complete else "INCOMPLETE",
            round(total_pnl, 6),
            "persisted total_pnl_usd; not independently recalculated",
            as_of,
            "stored data does not identify the runtime mode"
            if aggregate_pnl_complete
            else "aggregate fee accounting version is missing or incomplete",
        )
        if total_pnl is not None
        else _unknown("persisted state", "recorded PnL is missing or malformed", as_of)
    )

    open_rows = _position_list(state, "open_positions")
    closed_rows = _position_list(state, "closed_positions")
    trade_rows = _position_list(state, "trades")
    if open_rows is None or closed_rows is None or trade_rows is None:
        fee_point = _unknown("persisted state", "position history is missing or malformed")
    else:
        positions = [*open_rows, *closed_rows]
        known_fees = [
            _finite_number(position.get("simulated_fees_usd"))
            for position in positions
        ]
        history_flag = state.get("pnl_fee_history_incomplete")
        complete = aggregate_pnl_complete
        pnl_trades = [trade for trade in trade_rows if "pnl_usd" in trade or "fees_usd" in trade]
        fee_sum = _safe_sum([value for value in known_fees if value is not None])
        recorded = (
            round(fee_sum, 6)
            if fee_sum is not None and (
                any(value is not None for value in known_fees)
                or (history_flag is False and not positions and not pnl_trades)
            )
            else None
        )
        if complete:
            fee_point = _point(
                "OBSERVED",
                {"recorded_usd": recorded, "history_complete": True},
                "persisted fee fields",
                as_of,
                "stored data does not identify the runtime mode",
            )
        elif history_flag is True or history_flag is False or any(value is not None for value in known_fees):
            fee_point = _point(
                "INCOMPLETE",
                {"recorded_usd": recorded, "history_complete": False},
                "persisted fee fields",
                as_of,
                "recorded subtotal only; full fee history is not verified",
            )
        else:
            fee_point = _unknown("persisted state", "fee history is not established")

    return {
        "emergency_stop": stop_point,
        "open_positions": count_point,
        "exposure_cost_basis_usd": exposure_point,
        "pnl_recorded_usd": pnl_point,
        "unrealized_pnl_usd": _unknown("runtime price feed", "no current price feed is persisted"),
        "fee_history": fee_point,
    }


def _read_helius_store(path: Path | None) -> tuple[dict[str, Any] | None, str | None, str]:
    if path is None:
        return None, None, "event-store path not configured"
    try:
        if not path.is_file():
            return None, None, "event-store unavailable"
        as_of = _as_of(path, include_wal=True)
        with closing(_readonly_connection(path)) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not {"event_streams", "event_inbox", "event_counters"}.issubset(tables):
                return None, as_of, "event-store schema unavailable"
            counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM event_inbox GROUP BY status"
                )
            }
            byte_row = connection.execute(
                "SELECT COALESCE(SUM(length(payload_json)), 0) AS size "
                "FROM event_inbox WHERE status != 'processed'"
            ).fetchone()
            stream_row = connection.execute(
                "SELECT COUNT(*) AS streams, "
                "COALESCE(SUM(CASE WHEN checkpoint_sequence > 0 THEN 1 ELSE 0 END), 0) AS checkpointed, "
                "COALESCE(SUM(recovery_required), 0) AS recovery_required, "
                "COALESCE(SUM(intake_paused), 0) AS intake_paused FROM event_streams"
            ).fetchone()
            freshness_row = connection.execute(
                "SELECT MAX(received_at) AS latest_received, MAX(processed_at) AS latest_processed "
                "FROM event_inbox"
            ).fetchone()
            raw_counters = {
                str(row["name"]): int(row["value"])
                for row in connection.execute(
                    "SELECT name, value FROM event_counters "
                    "WHERE name IN ('received','duplicates','replayed','retried','queue_full')"
                )
            }
        pending = sum(counts.get(status, 0) for status in ("pending", "retry", "processing", "unknown"))
        return {
            "pending": pending,
            "processed": counts.get("processed", 0),
            "unknown": counts.get("unknown", 0),
            "pending_bytes": int(byte_row["size"]),
            "received": raw_counters.get("received", 0),
            "duplicates": raw_counters.get("duplicates", 0),
            "replayed": raw_counters.get("replayed", 0),
            "retried": raw_counters.get("retried", 0),
            "queue_full": raw_counters.get("queue_full", 0),
            "streams": int(stream_row["streams"]),
            "checkpointed_streams": int(stream_row["checkpointed"]),
            "recovery_required_streams": int(stream_row["recovery_required"]),
            "intake_paused_streams": int(stream_row["intake_paused"]),
            "latest_received_at_utc": _epoch_as_utc(freshness_row["latest_received"]),
            "latest_processed_at_utc": _epoch_as_utc(freshness_row["latest_processed"]),
        }, as_of, "read-only aggregate from Helius event store"
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None, _as_of(path, include_wal=True), "event-store read failed"


def _read_bounded_lines(path: Path) -> tuple[list[bytes], str | None, bool]:
    try:
        if not path.is_file():
            return [], None, False
        size = path.stat().st_size
        truncated = size > _MAX_METRICS_FILE_BYTES
        with path.open("rb") as stream:
            stream.seek(max(0, size - _MAX_METRICS_FILE_BYTES))
            data = stream.read(_MAX_METRICS_FILE_BYTES)
        if truncated:
            first_newline = data.find(b"\n")
            data = data[first_newline + 1:] if first_newline >= 0 else b""
        return data.splitlines(), _as_of(path), truncated
    except OSError:
        return [], None, False


def _valid_metric(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    stage = item.get("stage")
    provider = item.get("provider", "unknown")
    outcome = item.get("outcome")
    duration = _finite_number(item.get("duration_ms"))
    timestamp = _finite_number(item.get("ts"))
    if (
        not isinstance(stage, str) or not _METRIC_LABEL.fullmatch(stage)
        or stage not in _ALLOWED_STAGES
        or not isinstance(provider, str) or not _METRIC_LABEL.fullmatch(provider)
        or provider not in _ALLOWED_PROVIDERS
        or not isinstance(outcome, str) or outcome not in _ALLOWED_OUTCOMES
        or duration is None or duration < 0
        or timestamp is None or timestamp < 0
    ):
        return None
    return {
        "stage": stage,
        "provider": provider,
        "outcome": outcome,
        "duration_ms": duration,
        "ts": timestamp,
    }


def _percentile(values: list[float], quantile: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    rank = (len(ordered) - 1) * quantile
    low = math.floor(rank)
    high = math.ceil(rank)
    result = ordered[low] if low == high else ordered[low] + (ordered[high] - ordered[low]) * (rank - low)
    return round(result, 3)


def _read_metrics(path: Path | None) -> tuple[dict[str, Any] | None, str | None, str]:
    if path is None:
        return None, None, "metrics path not configured"
    events: deque[dict[str, Any]] = deque(maxlen=_MAX_METRIC_EVENTS)
    as_of: str | None = None
    found = False
    invalid_lines = 0
    truncated_files = 0
    valid_events_seen = 0
    for source in (path.with_suffix(path.suffix + ".1"), path):
        lines, source_as_of, truncated = _read_bounded_lines(source)
        truncated_files += int(truncated)
        if source_as_of is not None:
            found = True
            if as_of is None or source_as_of > as_of:
                as_of = source_as_of
        for line in lines:
            if not line.strip():
                continue
            try:
                item = json.loads(line.decode("utf-8"), parse_constant=_reject_json_constant)
            except (UnicodeError, json.JSONDecodeError, ValueError):
                invalid_lines += 1
                continue
            event = _valid_metric(item)
            if event is not None:
                events.append(event)
                valid_events_seen += 1
            else:
                invalid_lines += 1
    window_limited = valid_events_seen > _MAX_METRIC_EVENTS
    if not found:
        return None, None, "metrics file unavailable"

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[(event["stage"], event["provider"])].append(event)
    stages = []
    failures = 0
    for (stage, provider), rows in sorted(grouped.items()):
        durations = [row["duration_ms"] for row in rows]
        failures += sum(row["outcome"] in {"failure", "unknown"} for row in rows)
        stages.append({
            "stage": stage,
            "provider": provider,
            "count": len(rows),
            "failure_rate": round(sum(row["outcome"] in {"failure", "unknown"} for row in rows) / len(rows), 4),
            "p50_ms": _percentile(durations, 0.50),
            "p95_ms": _percentile(durations, 0.95),
            "p99_ms": _percentile(durations, 0.99),
        })
    return {
        "events": len(events),
        "stages": stages[:12],
        "observed_failure_or_unknown_events": failures,
        "invalid_lines_omitted": invalid_lines,
        "truncated_files": truncated_files,
        "window_limited": window_limited,
        "dropped_event_count": None,
        "dropped_event_status": "UNKNOWN",
        "quality": "INCOMPLETE" if invalid_lines or truncated_files or window_limited else "OBSERVED",
    }, as_of, "sanitized local execution-metrics file"


def build_snapshot(
    state_path: str | Path | None = "sniper_data.sqlite3",
    helius_path: str | Path | None = "data/helius_events.sqlite3",
    metrics_path: str | Path | None = "data/execution_metrics.jsonl",
) -> dict[str, Any]:
    """Read explicitly configured local sources and return aggregate status only."""
    state_file = Path(state_path) if state_path is not None else None
    helius_file = Path(helius_path) if helius_path is not None else None
    metrics_file = Path(metrics_path) if metrics_path is not None else None
    state, state_as_of, state_detail = _read_state(state_file)
    points = _state_points(state, state_as_of, state_detail)
    helius, helius_as_of, helius_detail = _read_helius_store(helius_file)
    metrics, metrics_as_of, metrics_detail = _read_metrics(metrics_file)
    if metrics is not None:
        metric_notes = []
        if metrics["invalid_lines_omitted"]:
            metric_notes.append("invalid entries omitted")
        if metrics["truncated_files"]:
            metric_notes.append("file-size limit applied; older or partial data omitted")
        if metrics["window_limited"]:
            metric_notes.append("latest bounded event window shown")
        metrics_detail = "; ".join(metric_notes) if metric_notes else metrics_detail

    points.update({
        "runtime_mode": _unknown("runtime configuration", "configuration is not loaded by this dashboard"),
        "runtime_heartbeat": _unknown("runtime heartbeat", "no heartbeat source is persisted"),
        "signals": _unknown("signal store", "no canonical signal snapshot is persisted on this base"),
        "helius_inbox": (
            _point("OBSERVED", helius, "Helius SQLite inbox", helius_as_of, helius_detail)
            if helius is not None else _unknown("Helius SQLite inbox", helius_detail)
        ),
        "helius_event_freshness": (
            _point(
                "OBSERVED",
                {"latest_received_at_utc": helius["latest_received_at_utc"],
                 "latest_processed_at_utc": helius["latest_processed_at_utc"]},
                "Helius event inbox timestamps",
                helius_as_of,
                "age thresholds are not configured; no freshness verdict is inferred",
            )
            if helius is not None else _unknown("Helius event inbox timestamps", helius_detail)
        ),
        "helius_checkpoints": (
            _point(
                "DEGRADED" if helius["recovery_required_streams"] or helius["intake_paused_streams"] else "OBSERVED",
                {key: helius[key] for key in ("streams", "checkpointed_streams", "recovery_required_streams", "intake_paused_streams")},
                "Helius event-stream checkpoint aggregates",
                helius_as_of,
                helius_detail,
            )
            if helius is not None else _unknown("Helius event-stream checkpoint aggregates", helius_detail)
        ),
        "helius_provider_health": _unknown("Helius provider health", "no provider heartbeat or active health probe is persisted"),
        "rpc_health": _unknown("RPC health", "RPC counters are process-local and no heartbeat is persisted"),
        "telegram_delivery": _unknown("Telegram delivery", "no delivery-health snapshot is persisted"),
        "performance": (
            _point(
                metrics["quality"],
                metrics,
                "execution metrics",
                metrics_as_of,
                metrics_detail,
            )
            if metrics is not None else _unknown("execution metrics", metrics_detail)
        ),
        "observed_errors": (
            _point(
                metrics["quality"],
                metrics["observed_failure_or_unknown_events"],
                "failure/unknown outcomes in sanitized execution metrics only",
                metrics_as_of,
                "not a count of all runtime errors",
            )
            if metrics is not None else _unknown("execution metrics", metrics_detail)
        ),
    })
    return {"generated_at_utc": _utc_now(), **points}


def create_server(
    state_path: str | Path | None = "sniper_data.sqlite3",
    helius_path: str | Path | None = "data/helius_events.sqlite3",
    metrics_path: str | Path | None = "data/execution_metrics.jsonl",
    *,
    port: int = 8765,
) -> ThreadingHTTPServer:
    """Create a loopback-only HTTP server exposing a read-only status page."""
    def respond(handler: BaseHTTPRequestHandler, status: int, content_type: str, body: bytes) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'none'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'",
        )
        handler.end_headers()
        handler.wfile.write(body)
        handler.close_connection = True

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "PaperDashboard"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            self.request.settimeout(2.0)
            super().setup()

        def log_message(self, _format: str, *args: Any) -> None:
            return

        def _local_host(self) -> bool:
            host = self.headers.get("Host", "")
            hostname = host.rsplit(":", 1)[0].strip("[]").lower() if ":" in host else host.lower()
            return hostname in {"127.0.0.1", "localhost"}

        def do_GET(self) -> None:
            if not self._local_host():
                respond(self, 400, "text/plain; charset=utf-8", b"Bad request")
                return
            route = urlsplit(self.path).path
            if route == "/":
                respond(self, 200, "text/html; charset=utf-8", _PAGE.encode("utf-8"))
            elif route == "/api/status":
                body = json.dumps(
                    build_snapshot(state_path, helius_path, metrics_path),
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                respond(self, 200, "application/json; charset=utf-8", body)
            else:
                respond(self, 404, "text/plain; charset=utf-8", b"Not found")

        def do_POST(self) -> None:
            self.close_connection = True
            respond(self, 405, "text/plain; charset=utf-8", b"Method not allowed")

        def do_PUT(self) -> None:
            self.do_POST()

        def do_DELETE(self) -> None:
            self.do_POST()

        def do_PATCH(self) -> None:
            self.do_POST()

    class DashboardServer(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = False

    return DashboardServer(("127.0.0.1", port), DashboardHandler)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve a local read-only PAPER status dashboard.")
    parser.add_argument("--state-path", default="sniper_data.sqlite3", help="SQLite state DB or legacy JSON snapshot")
    parser.add_argument("--helius-db", default="data/helius_events.sqlite3", help="Helius event-store SQLite path")
    parser.add_argument("--metrics-file", default="data/execution_metrics.jsonl", help="Execution-metrics JSONL path")
    parser.add_argument("--port", type=int, default=8765, help="loopback port (default: 8765)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return args


def main() -> int:
    args = _parse_args()
    server = create_server(args.state_path, args.helius_db, args.metrics_file, port=args.port)
    print(f"Read-only PAPER snapshot viewer: http://127.0.0.1:{server.server_port}/")
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
