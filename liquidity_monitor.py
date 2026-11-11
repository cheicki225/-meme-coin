"""Conservative, read-only DexScreener liquidity change monitor.

This is an alerting heuristic, not proof of a rug or a liquidity-lock verifier.
It deliberately never submits sells or changes trading decisions.
"""
from __future__ import annotations

import math
import time
from typing import Any


def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def extract_liquidity_snapshot(token_mint: str, pair: Any, sampled_at: float | None = None) -> dict | None:
    """Extract a trustworthy-enough Solana pair liquidity observation."""
    if not isinstance(pair, dict) or str(pair.get("chainId", "")).lower() != "solana":
        return None
    pair_address = pair.get("pairAddress")
    if not isinstance(pair_address, str) or not pair_address.strip():
        return None
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    if token_mint not in {base.get("address"), quote.get("address")}:
        return None
    liquidity = pair.get("liquidity") or {}
    liquidity_usd = _finite_positive(liquidity.get("usd") if isinstance(liquidity, dict) else None)
    if liquidity_usd is None:
        return None
    return {
        "token_mint": token_mint,
        "pair_address": pair_address,
        "dex_id": str(pair.get("dexId") or "unknown"),
        "liquidity_usd": liquidity_usd,
        "sampled_at": float(sampled_at if sampled_at is not None else time.time()),
    }


def assess_liquidity_change(
    previous: dict | None,
    current: dict | None,
    *,
    threshold_pct: float = 40.0,
) -> dict:
    """Compare two observations from the same pool; never infer across pool changes."""
    try:
        threshold = float(threshold_pct)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("threshold_pct must be a finite number between 1 and 99")
    if not math.isfinite(threshold) or not 1 <= threshold <= 99:
        raise ValueError("threshold_pct must be a finite number between 1 and 99")
    if not isinstance(current, dict):
        return {"status": "unavailable", "alert": False}
    if not isinstance(previous, dict):
        return {"status": "baseline", "alert": False}
    if previous.get("pair_address") != current.get("pair_address"):
        return {"status": "pool_changed", "alert": True}
    old = _finite_positive(previous.get("liquidity_usd"))
    new = _finite_positive(current.get("liquidity_usd"))
    if old is None or new is None:
        return {"status": "unavailable", "alert": False}
    drop_pct = max(0.0, (old - new) / old * 100.0)
    return {
        "status": "liquidity_drop" if drop_pct >= threshold else "ok",
        "alert": drop_pct >= threshold,
        "drop_pct": drop_pct,
        "previous_liquidity_usd": old,
        "current_liquidity_usd": new,
        "threshold_pct": threshold,
    }


def update_position_liquidity(position: dict, pair: Any, *, threshold_pct: float = 40.0, sampled_at: float | None = None) -> dict:
    """Update the per-position baseline and return a one-shot alert transition."""
    current = extract_liquidity_snapshot(position.get("token_mint", ""), pair, sampled_at)
    if current is None:
        return {"status": "unavailable", "alert": False}
    monitor = position.get("liquidity_monitor")
    if not isinstance(monitor, dict):
        monitor = {}
    previous = monitor.get("last_snapshot")
    result = assess_liquidity_change(previous, current, threshold_pct=threshold_pct)
    was_alerting = bool(monitor.get("alert_active"))
    alert_now = bool(result.get("alert")) and not was_alerting
    monitor["last_snapshot"] = current
    monitor["alert_active"] = bool(result.get("alert"))
    monitor["last_status"] = result["status"]
    position["liquidity_monitor"] = monitor
    result["alert"] = alert_now
    result["snapshot"] = current
    return result
