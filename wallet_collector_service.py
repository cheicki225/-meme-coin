"""Scheduled, read-only collection of activity from monitored wallets."""
from __future__ import annotations

import asyncio
import logging
import time

import config

log = logging.getLogger("wallet_collector_service")


async def _collect_wallet(wallet: str, limit: int, db_path: str):
    # Delayed import keeps the service testable without network adapters and
    # avoids importing collection code when this optional feature is disabled.
    import wallet_data_collector
    return await wallet_data_collector.collect_wallet(wallet, limit=limit, db_path=db_path)


class WalletCollectorService:
    """Round-robin collector that uses only read-only Solana RPC methods."""

    def __init__(self, data_store, notifier=None):
        self.data_store = data_store
        self.notifier = notifier
        self._last_alert_at = 0.0
        self._cycle_lock = asyncio.Lock()

    async def run_once(self) -> dict:
        async with self._cycle_lock:
            wallets = sorted(
                address for address in self.data_store.state.get("monitored_dev_wallets", {})
                if isinstance(address, str) and address
            )
            if not wallets:
                return {"status": "idle", "wallets_scanned": 0, "events_detected": 0}

            maximum = max(1, min(int(config.WALLET_COLLECTOR_MAX_WALLETS_PER_CYCLE), len(wallets)))
            limit = max(1, min(int(config.WALLET_COLLECTOR_SIGNATURES_PER_WALLET), 1000))
            start = int(self.data_store.state.get("wallet_collector_next_index", 0)) % len(wallets)
            selected = [wallets[(start + offset) % len(wallets)] for offset in range(maximum)]
            results = []
            next_index = (start + maximum) % len(wallets)

            for address in selected:
                try:
                    result = await _collect_wallet(address, limit, config.WALLET_ACTIVITY_DB_FILE)
                except Exception as exc:
                    log.exception("Read-only collection failed for monitored wallet")
                    result = {"status": "error", "reason": type(exc).__name__}
                results.append(result)
                if result.get("status") in {"error", "unavailable"}:
                    await self._alert_failure(result.get("reason", result.get("status")))

            self.data_store.state["wallet_collector_next_index"] = next_index
            self.data_store.save()
            return {
                "status": "partial" if any(item.get("status") in {"error", "partial"} for item in results) else "ok",
                "wallets_scanned": len(selected),
                "events_detected": sum(int(item.get("events_detected", 0) or 0) for item in results),
                "results": results,
            }

    async def _alert_failure(self, reason: str) -> None:
        now = time.monotonic()
        if not self.notifier or now - self._last_alert_at < 900:
            return
        self._last_alert_at = now
        await self.notifier.notify(
            "collector_alert",
            "⚠️ Collecteur wallets indisponible. Aucune transaction n'a été envoyée. "
            f"Code de diagnostic: {str(reason)[:80]}",
        )

    async def start(self) -> None:
        if not config.WALLET_COLLECTOR_ENABLED:
            log.info("Wallet data collector disabled by configuration")
            return
        if not config.HELIUS_API_KEY:
            await self._alert_failure("helius_not_configured")
            return
        interval = max(30, int(config.WALLET_COLLECTOR_INTERVAL_S))
        while True:
            await self.run_once()
            await asyncio.sleep(interval)

