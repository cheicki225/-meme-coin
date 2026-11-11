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
        self._last_alert_at = 0.0

    async def run_once(self) -> dict:
        metrics = rpc_client.get_metrics()
        total = int(metrics.get("requests", 0))
        errors = int(metrics.get("errors", 0))
        error_rate = errors / total if total else 0.0
        should_alert = total >= self.minimum_requests and error_rate >= self.error_rate_threshold
        if should_alert and self.notifier and time.monotonic() - self._last_alert_at >= self.cooldown_s:
            self._last_alert_at = time.monotonic()
            methods = sorted(metrics.get("errors_by_method", {}).items(), key=lambda item: item[1], reverse=True)[:3]
            summary = ", ".join(f"{method}: {count}" for method, count in methods) or "non détaillé"
            await self.notifier.notify(
                "rpc_alert",
                f"⚠️ *Santé RPC dégradée*\nÉchecs: {error_rate:.0%} ({errors}/{total}). "
                f"Méthodes les plus touchées: {summary}.",
            )
        return {"requests": total, "errors": errors, "error_rate": error_rate, "alert": should_alert}

    async def start(self, interval_s: int = 60) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(max(15, interval_s))

