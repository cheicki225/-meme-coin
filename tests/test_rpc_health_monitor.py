import asyncio
from types import SimpleNamespace

import rpc_health_monitor


class Notifier:
    def __init__(self, result=None):
        self.messages = []
        self.result = result

    async def notify(self, category, text):
        self.messages.append((category, text))
        if isinstance(self.result, Exception):
            raise self.result
        if callable(self.result):
            return self.result()
        return self.result


def test_rpc_monitor_alerts_on_repeated_failures_and_applies_cooldown(monkeypatch):
    notifier = Notifier()
    snapshots = iter([
        {"requests": 20, "errors": 8, "errors_by_method": {"getTransaction": 6, "getBalance": 2}},
        {"requests": 40, "errors": 16, "errors_by_method": {"getTransaction": 12, "getBalance": 4}},
        {"requests": 60, "errors": 24, "errors_by_method": {"getTransaction": 18, "getBalance": 6}},
    ])
    monkeypatch.setattr(rpc_health_monitor.rpc_client, "get_metrics", lambda: next(snapshots), raising=False)
    clock = iter([1000.0, 1000.0, 1601.0])
    monkeypatch.setattr(rpc_health_monitor, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monitor = rpc_health_monitor.RpcHealthMonitor(notifier, cooldown_s=600)

    first = asyncio.run(monitor.run_once())
    second = asyncio.run(monitor.run_once())
    third = asyncio.run(monitor.run_once())

    assert first["error_rate"] == 0.4
    assert first["requests"] == 20
    assert first["errors"] == 8
    assert second["alert"] is True
    assert third["alert"] is True
    assert first["notified"] is True
    assert second["notified"] is False
    assert third["notified"] is True
    assert len(notifier.messages) == 2
    assert notifier.messages[0][0] == "rpc_alert"


def test_rpc_monitor_ignores_small_sample(monkeypatch):
    notifier = Notifier()
    monkeypatch.setattr(rpc_health_monitor.rpc_client, "get_metrics", lambda: {
        "requests": 2, "errors": 2, "errors_by_method": {},
    }, raising=False)
    result = asyncio.run(rpc_health_monitor.RpcHealthMonitor(notifier).run_once())
    assert result["alert"] is False
    assert result["notified"] is False
    assert notifier.messages == []


def test_rpc_monitor_retries_alert_when_telegram_delivery_fails(monkeypatch):
    notifier = Notifier(result=iter([False, True]).__next__)
    snapshots = iter([
        {"requests": 20, "errors": 8, "errors_by_method": {"getTransaction": 6}},
        {"requests": 40, "errors": 16, "errors_by_method": {"getTransaction": 12}},
    ])
    monkeypatch.setattr(rpc_health_monitor.rpc_client, "get_metrics", lambda: next(snapshots), raising=False)
    clock = iter([1000.0, 1001.0])
    monkeypatch.setattr(rpc_health_monitor, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monitor = rpc_health_monitor.RpcHealthMonitor(notifier, cooldown_s=600)

    first = asyncio.run(monitor.run_once())
    second = asyncio.run(monitor.run_once())

    assert first["alert"] is True and first["notified"] is False
    assert second["alert"] is True and second["notified"] is True
    assert len(notifier.messages) == 2


def test_rpc_monitor_survives_unexpected_notifier_error_and_does_not_log_details(monkeypatch, caplog):
    notifier = Notifier(result=RuntimeError("secret provider URL should not be logged"))
    monkeypatch.setattr(rpc_health_monitor.rpc_client, "get_metrics", lambda: {
        "requests": 20, "errors": 8, "errors_by_method": {},
    }, raising=False)
    monkeypatch.setattr(rpc_health_monitor, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    monitor = rpc_health_monitor.RpcHealthMonitor(notifier)

    result = asyncio.run(monitor.run_once())

    assert result["alert"] is True and result["notified"] is False
    assert "secret provider URL" not in caplog.text


def test_rpc_monitor_measures_each_poll_delta_instead_of_cumulative_history(monkeypatch):
    notifier = Notifier()
    snapshots = iter([
        {"requests": 20, "errors": 2, "errors_by_method": {"getBalance": 2}},
        {"requests": 30, "errors": 8, "errors_by_method": {"getBalance": 5, "getTransaction": 3}},
    ])
    monkeypatch.setattr(rpc_health_monitor.rpc_client, "get_metrics", lambda: next(snapshots), raising=False)
    monkeypatch.setattr(rpc_health_monitor, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    monitor = rpc_health_monitor.RpcHealthMonitor(notifier, minimum_requests=10)

    first = asyncio.run(monitor.run_once())
    second = asyncio.run(monitor.run_once())

    assert first["error_rate"] == 0.1
    assert first["alert"] is False
    assert second["requests"] == 10
    assert second["errors"] == 6
    assert second["error_rate"] == 0.6
    assert second["alert"] is True
    assert len(notifier.messages) == 1
    assert "getBalance: 3" in notifier.messages[0][1]
    assert "getTransaction: 3" in notifier.messages[0][1]


def test_rpc_monitor_starts_new_window_after_counter_reset(monkeypatch):
    notifier = Notifier()
    snapshots = iter([
        {"requests": 20, "errors": 1, "errors_by_method": {"getBalance": 1}},
        {"requests": 4, "errors": 2, "errors_by_method": {"getTransaction": 2}},
    ])
    monkeypatch.setattr(rpc_health_monitor.rpc_client, "get_metrics", lambda: next(snapshots), raising=False)
    monkeypatch.setattr(rpc_health_monitor, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    monitor = rpc_health_monitor.RpcHealthMonitor(notifier, minimum_requests=3)

    asyncio.run(monitor.run_once())
    after_reset = asyncio.run(monitor.run_once())

    assert after_reset["counter_reset"] is True
    assert after_reset["requests"] == 4
    assert after_reset["errors"] == 2
    assert after_reset["error_rate"] == 0.5
    assert after_reset["alert"] is True
    assert "getTransaction: 2" in notifier.messages[0][1]


def test_rpc_monitor_ignores_duplicate_snapshot_with_no_new_rpc_requests(monkeypatch):
    notifier = Notifier()
    snapshot = {"requests": 20, "errors": 8, "errors_by_method": {"getTransaction": 8}}
    monkeypatch.setattr(rpc_health_monitor.rpc_client, "get_metrics", lambda: snapshot, raising=False)
    # The first alert is allowed even when monotonic uptime is below cooldown.
    monkeypatch.setattr(rpc_health_monitor, "time", SimpleNamespace(monotonic=lambda: 1.0))
    monitor = rpc_health_monitor.RpcHealthMonitor(notifier)

    first = asyncio.run(monitor.run_once())
    second = asyncio.run(monitor.run_once())

    assert first["alert"] is True
    assert second["requests"] == 0
    assert second["errors"] == 0
    assert second["alert"] is False
    assert len(notifier.messages) == 1

