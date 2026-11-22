import asyncio

import websocket_listener
from websocket_listener import NewTokenListener


def test_reconnect_delay_is_exponential_jittered_and_bounded(monkeypatch):
    monkeypatch.setattr(websocket_listener.random, "uniform", lambda low, high: high)

    assert NewTokenListener._reconnect_delay(0) == 0.625
    assert NewTokenListener._reconnect_delay(100) == 30.0


def test_normal_websocket_close_uses_reconnect_delay(monkeypatch, tmp_path):
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    monkeypatch.setattr(websocket_listener.config, "HELIUS_WS_URL", "wss://unused.invalid")
    listener = NewTokenListener(on_new_token=lambda _event: None)
    calls = 0
    delays = []

    async def listen_once():
        nonlocal calls
        calls += 1
        if calls == 2:
            listener._running = False

    async def wait_before_reconnect(delay):
        delays.append(delay)
        return True

    monkeypatch.setattr(listener, "_listen_once", listen_once)
    monkeypatch.setattr(listener, "_reconnect_delay", lambda _attempt: 0.25)
    monkeypatch.setattr(listener, "_wait_before_reconnect", wait_before_reconnect)

    asyncio.run(listener.start())

    assert calls == 2
    assert delays == [0.25]


def test_stop_closes_current_socket_and_wakes_backoff():
    class FakeSocket:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    listener = NewTokenListener(on_new_token=lambda _event: None)
    socket = FakeSocket()

    async def stop_listener():
        listener._running = True
        listener._stop_event = asyncio.Event()
        listener._ws = socket
        await listener.stop()
        assert listener._stop_event.is_set()

    asyncio.run(stop_listener())

    assert not listener._running
    assert socket.closed


def test_stop_interrupts_reconnect_wait():
    listener = NewTokenListener(on_new_token=lambda _event: None)

    async def wait_after_stop():
        listener._stop_event = asyncio.Event()
        listener._stop_event.set()
        return await listener._wait_before_reconnect(30)

    assert asyncio.run(wait_after_stop()) is False
