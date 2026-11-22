"""Periodic RPC health alerts with a cooldown to avoid Telegram spam."""
from __future__ import annotations

import asyncio
import logging
import time

import config
import rpc_client

log = logging.getLogger("rpc_health_monitor")


class RpcHealthMonitor:
    def __init__(self, notifier=None, *, minimum_requests: int = 10,
                 error_rate_threshold: float = 0.25, cooldown_s: int = 600):
        self.notifier = notifier
        self.minimum_requests = minimum_requests
        self.error_rate_threshold = error_rate_threshold
        self.cooldown_s = cooldown_s
        self._last_alert_at: float | None = None
        self._last_metrics_snapshot: dict | None = None

    @staticmethod
    def _counter(value) -> int:
        if type(value) is int and value >= 0:
            return value
        return 0

    def _window_metrics(self, metrics: dict) -> tuple[int, int, dict[str, int], bool]:
        """Return counter deltas since the prior poll, tolerating resets.

        RPC counters are process-wide and may be shared by other consumers, so
        this monitor keeps a private baseline instead of resetting them. If a
        counter decreases (for example after `rpc_client.reset_metrics()`), its
        current value starts the new window.
        """
        current_methods = metrics.get("errors_by_method", {})
        if not isinstance(current_methods, dict):
            current_methods = {}
        current = {
            "requests": self._counter(metrics.get("requests", 0)),
            "errors": self._counter(metrics.get("errors", 0)),
            "errors_by_method": {
                method: self._counter(count)
                for method, count in current_methods.items()
                if isinstance(method, str)
            },
        }
        previous = self._last_metrics_snapshot
        self._last_metrics_snapshot = current
        if previous is None:
            return current["requests"], current["errors"], current["errors_by_method"], False

        previous_methods = previous["errors_by_method"]
        counter_reset = (
            current["requests"] < previous["requests"]
            or current["errors"] < previous["errors"]
            or any(
                current["errors_by_method"].get(method, 0) < count
                for method, count in previous_methods.items()
            )
        )
        if counter_reset:
            # The RPC client reset_metrics() clears all counters together.
            # Use the complete new snapshot to avoid mixing epochs.
            return current["requests"], current["errors"], current["errors_by_method"], True

        requests = current["requests"] - previous["requests"]
        errors = current["errors"] - previous["errors"]
        methods = {
            method: current["errors_by_method"].get(method, 0) - previous_methods.get(method, 0)
            for method in set(current["errors_by_method"]) | set(previous_methods)
        }
        return requests, errors, methods, False

    async def run_once(self) -> dict:
        metrics = rpc_client.get_metrics()
        total, errors, errors_by_method, counter_reset = self._window_metrics(metrics)
        error_rate = errors / total if total else 0.0
        should_alert = total >= self.minimum_requests and error_rate >= self.error_rate_threshold
        notified = False
        now = time.monotonic()
        cooldown_elapsed = (
            self._last_alert_at is None
            or now - self._last_alert_at >= self.cooldown_s
        )
        if should_alert and self.notifier and cooldown_elapsed:
            methods = sorted(
                ((method, count) for method, count in errors_by_method.items() if count > 0),
                key=lambda item: item[1],
                reverse=True,
            )[:3]
            summary = ", ".join(f"{method}: {count}" for method, count in methods) or "non détaillé"
            try:
                delivery_result = await self.notifier.notify(
                    "rpc_alert",
                    f"⚠️ *Santé RPC dégradée*\nÉchecs: {error_rate:.0%} ({errors}/{total}). "
                    f"Méthodes les plus touchées: {summary}.",
                )
                # Notifier returns False when Telegram is unavailable. Treat
                # None as success for compatibility with existing notifier adapters.
                notified = delivery_result is not False
            except Exception as exc:
                # Keep the monitor loop alive and avoid logging exception text,
                # which may contain a provider URL or credentials.
                log.warning("RPC health alert delivery failed (%s)", type(exc).__name__)
            if notified:
                self._last_alert_at = now
        return {
            "requests": total,
            "errors": errors,
            "error_rate": error_rate,
            "alert": should_alert,
            "notified": notified,
            "counter_reset": counter_reset,
        }

    async def start(self, interval_s: int = 60) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(max(15, interval_s))

