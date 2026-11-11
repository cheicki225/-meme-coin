import asyncio
from types import SimpleNamespace

import rpc_health_monitor


class Notifier:
    def __init__(self):
        self.messages = []

    async def notify(self, category, text):
        self.messages.append((category, text))


def test_rpc_monitor_alerts_on_repeated_failures_and_applies_cooldown(monkeypatch):
    notifier = Notifier()
    monkeypatch.setattr(rpc_health_monitor.rpc_client, "get_metrics", lambda: {
        "requests": 20, "errors": 8, "errors_by_method": {"getTransaction": 6, "getBalance": 2},
    }, raising=False)
    clock = iter([1000.0, 1000.0, 1001.0, 1601.0, 1601.0])
    monkeypatch.setattr(rpc_health_monitor, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monitor = rpc_health_monitor.RpcHealthMonitor(notifier, cooldown_s=600)

    first = asyncio.run(monitor.run_once())
    second = asyncio.run(monitor.run_once())
    third = asyncio.run(monitor.run_once())

    assert first["error_rate"] == 0.4
    assert second["alert"] is True
    assert third["alert"] is True
    assert len(notifier.messages) == 2
    assert notifier.messages[0][0] == "rpc_alert"


def test_rpc_monitor_ignores_small_sample(monkeypatch):
    notifier = Notifier()
    monkeypatch.setattr(rpc_health_monitor.rpc_client, "get_metrics", lambda: {
        "requests": 2, "errors": 2, "errors_by_method": {},
    }, raising=False)
    result = asyncio.run(rpc_health_monitor.RpcHealthMonitor(notifier).run_once())
    assert result["alert"] is False
    assert notifier.messages == []

