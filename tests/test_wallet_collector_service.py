import asyncio
from types import SimpleNamespace

import config
import wallet_collector_service as service


class Store:
    def __init__(self):
        self.state = {"monitored_dev_wallets": {f"wallet-{n}": {} for n in range(5)}}
        self.saves = 0

    def save(self):
        self.saves += 1


class Notifier:
    def __init__(self, results=None):
        self.messages = []
        self.results = iter(results or [])

    async def notify(self, category, message):
        self.messages.append((category, message))
        result = next(self.results, None)
        if isinstance(result, Exception):
            raise result
        return result


def test_collector_rotates_wallets_and_persists_cursor(monkeypatch):
    store = Store()
    seen = []

    async def collect(wallet, limit, db_path):
        seen.append((wallet, limit, db_path))
        return {"status": "ok", "events_detected": 2}

    monkeypatch.setattr(service, "_collect_wallet", collect)
    monkeypatch.setattr(config, "WALLET_COLLECTOR_MAX_WALLETS_PER_CYCLE", 2, raising=False)
    monkeypatch.setattr(config, "WALLET_COLLECTOR_SIGNATURES_PER_WALLET", 15, raising=False)
    monkeypatch.setattr(config, "WALLET_ACTIVITY_DB_FILE", "wallets.sqlite3", raising=False)
    collector = service.WalletCollectorService(store)

    first = asyncio.run(collector.run_once())
    second = asyncio.run(collector.run_once())

    assert [item[0] for item in seen] == ["wallet-0", "wallet-1", "wallet-2", "wallet-3"]
    assert all(item[1:] == (15, "wallets.sqlite3") for item in seen)
    assert first["events_detected"] == 4
    assert second["wallets_scanned"] == 2
    assert store.state["wallet_collector_next_index"] == 4
    assert store.saves == 2


def test_collector_alerts_failures_without_exposing_provider_errors(monkeypatch):
    store = Store()
    notifier = Notifier()

    async def collect(wallet, limit, db_path):
        return {"status": "error", "reason": "signature_fetch_failed", "events_detected": 0}

    monkeypatch.setattr(service, "_collect_wallet", collect)
    monkeypatch.setattr(config, "WALLET_COLLECTOR_MAX_WALLETS_PER_CYCLE", 1, raising=False)
    monkeypatch.setattr(config, "WALLET_COLLECTOR_SIGNATURES_PER_WALLET", 10, raising=False)
    monkeypatch.setattr(config, "WALLET_ACTIVITY_DB_FILE", "wallets.sqlite3", raising=False)
    monkeypatch.setattr(service.time, "monotonic", lambda: 0.0)
    collector = service.WalletCollectorService(store, notifier)
    result = asyncio.run(collector.run_once())
    asyncio.run(collector.run_once())

    assert result["status"] == "partial"
    assert notifier.messages[0][0] == "collector_alert"
    assert "signature_fetch_failed" in notifier.messages[0][1]
    assert len(notifier.messages) == 1


def test_collector_alerts_on_partial_results_and_keeps_retry_cooldown(monkeypatch):
    store = Store()
    notifier = Notifier()

    async def collect(wallet, limit, db_path):
        return {
            "status": "partial", "reason": "transaction_fetch_partial",
            "transactions_unavailable": 1, "events_detected": 2,
        }

    monkeypatch.setattr(service, "_collect_wallet", collect)
    monkeypatch.setattr(config, "WALLET_COLLECTOR_MAX_WALLETS_PER_CYCLE", 1, raising=False)
    monkeypatch.setattr(config, "WALLET_COLLECTOR_SIGNATURES_PER_WALLET", 10, raising=False)
    monkeypatch.setattr(config, "WALLET_ACTIVITY_DB_FILE", "wallets.sqlite3", raising=False)
    monkeypatch.setattr(service.time, "monotonic", lambda: 0.0)
    collector = service.WalletCollectorService(store, notifier)

    result = asyncio.run(collector.run_once())

    assert result["status"] == "partial"
    assert notifier.messages[0][0] == "collector_alert"
    assert "transaction_fetch_partial" in notifier.messages[0][1]


def test_cursor_save_failure_rolls_back_in_memory_and_next_cycle_retries(monkeypatch):
    class FailingStore(Store):
        def __init__(self):
            super().__init__()
            self.fail_once = True

        def save(self):
            if self.fail_once:
                self.fail_once = False
                raise OSError("disk unavailable")
            super().save()

    store = FailingStore()
    seen = []

    async def collect(wallet, limit, db_path):
        seen.append(wallet)
        return {"status": "ok", "events_detected": 1}

    monkeypatch.setattr(service, "_collect_wallet", collect)
    monkeypatch.setattr(config, "WALLET_COLLECTOR_MAX_WALLETS_PER_CYCLE", 2, raising=False)
    monkeypatch.setattr(config, "WALLET_COLLECTOR_SIGNATURES_PER_WALLET", 10, raising=False)
    monkeypatch.setattr(config, "WALLET_ACTIVITY_DB_FILE", "wallets.sqlite3", raising=False)
    collector = service.WalletCollectorService(store)

    failed = asyncio.run(collector.run_once())
    assert failed["reason"] == "cursor_persistence_failed"
    assert failed["cursor_saved"] is False
    assert "wallet_collector_next_index" not in store.state

    succeeded = asyncio.run(collector.run_once())
    assert succeeded["cursor_saved"] is True
    assert seen == ["wallet-0", "wallet-1", "wallet-0", "wallet-1"]
    assert store.state["wallet_collector_next_index"] == 2


def test_collector_retries_alert_after_telegram_returns_false(monkeypatch):
    store = Store()
    notifier = Notifier([False, True])

    async def collect(wallet, limit, db_path):
        return {"status": "error", "reason": "signature_fetch_failed", "events_detected": 0}

    monkeypatch.setattr(service, "_collect_wallet", collect)
    monkeypatch.setattr(config, "WALLET_COLLECTOR_MAX_WALLETS_PER_CYCLE", 1, raising=False)
    monkeypatch.setattr(config, "WALLET_COLLECTOR_SIGNATURES_PER_WALLET", 10, raising=False)
    monkeypatch.setattr(config, "WALLET_ACTIVITY_DB_FILE", "wallets.sqlite3", raising=False)
    clock = iter([0.0, 1.0])
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    collector = service.WalletCollectorService(store, notifier)

    asyncio.run(collector.run_once())
    asyncio.run(collector.run_once())

    assert len(notifier.messages) == 2
    assert collector._last_alert_at == 1.0


def test_collector_retries_alert_after_notifier_exception_without_logging_details(monkeypatch, caplog):
    store = Store()
    notifier = Notifier([RuntimeError("provider URL secret must not be logged"), True])

    async def collect(wallet, limit, db_path):
        return {"status": "error", "reason": "signature_fetch_failed", "events_detected": 0}

    monkeypatch.setattr(service, "_collect_wallet", collect)
    monkeypatch.setattr(config, "WALLET_COLLECTOR_MAX_WALLETS_PER_CYCLE", 1, raising=False)
    monkeypatch.setattr(config, "WALLET_COLLECTOR_SIGNATURES_PER_WALLET", 10, raising=False)
    monkeypatch.setattr(config, "WALLET_ACTIVITY_DB_FILE", "wallets.sqlite3", raising=False)
    clock = iter([0.0, 1.0])
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    collector = service.WalletCollectorService(store, notifier)

    asyncio.run(collector.run_once())
    asyncio.run(collector.run_once())

    assert len(notifier.messages) == 2
    assert collector._last_alert_at == 1.0
    assert "provider URL secret" not in caplog.text
