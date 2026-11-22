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
    monkeypatch.setattr(rpc_health_monitor.rpc_client, "get_metrics", lambda: {
        "requests": 20, "errors": 8, "errors_by_method": {"getTransaction": 6, "getBalance": 2},
    }, raising=False)
    clock = iter([1000.0, 1000.0, 1601.0])
    monkeypatch.setattr(rpc_health_monitor, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monitor = rpc_health_monitor.RpcHealthMonitor(notifier, cooldown_s=600)

    first = asyncio.run(monitor.run_once())
    second = asyncio.run(monitor.run_once())
    third = asyncio.run(monitor.run_once())

    assert first["error_rate"] == 0.4
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
    monkeypatch.setattr(rpc_health_monitor.rpc_client, "get_metrics", lambda: {
        "requests": 20, "errors": 8, "errors_by_method": {"getTransaction": 6},
    }, raising=False)
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

