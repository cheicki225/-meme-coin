import asyncio

import config
import wallet_collector_service as service


class Store:
    def __init__(self):
        self.state = {"monitored_dev_wallets": {f"wallet-{n}": {} for n in range(5)}}
        self.saves = 0

    def save(self):
        self.saves += 1


class Notifier:
    def __init__(self):
        self.messages = []

    async def notify(self, category, message):
        self.messages.append((category, message))


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
    collector = service.WalletCollectorService(store, notifier)
    result = asyncio.run(collector.run_once())

    assert result["status"] == "partial"
    assert notifier.messages[0][0] == "collector_alert"
    assert "signature_fetch_failed" in notifier.messages[0][1]
    assert len(notifier.messages) == 1

