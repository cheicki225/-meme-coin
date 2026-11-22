import asyncio
import logging

import pytest

import rpc_client


@pytest.fixture(autouse=True)
def _isolate_metrics_path(monkeypatch, tmp_path):
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(tmp_path / "metrics.jsonl"))


def test_retry_delay_uses_exponential_backoff_and_retry_after(monkeypatch):
    monkeypatch.setattr(rpc_client.random, "uniform", lambda low, high: 0)
    assert rpc_client._retry_delay(0) == 0.5
    assert rpc_client._retry_delay(1) == 1.0
    assert rpc_client._retry_delay(0, "2") == 2
    assert rpc_client._retry_delay(0, "999") == 999


def test_only_transient_json_rpc_errors_are_retried():
    assert rpc_client._retryable_json_rpc_error({"code": -32603})
    assert rpc_client._retryable_json_rpc_error({"code": -32005})
    assert rpc_client._retryable_json_rpc_error({"code": -1, "message": "rate limit"})
    assert not rpc_client._retryable_json_rpc_error({"code": -32602, "message": "invalid params"})
    assert not rpc_client._retryable_json_rpc_error("credential=secret")


class _FakeResponse:
    def __init__(self, status, *, payload=None, headers=None):
        self.status = status
        self.payload = payload
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.payload


class _FakeSession:
    def __init__(self, responses):
        self.responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        return self.responses.pop(0)


class _FakeLimiter:
    def __init__(self):
        self.delays = []

    async def wait_if_needed(self):
        return True

    def defer(self, seconds):
        self.delays.append(seconds)


def test_rpc_retries_429_and_does_not_log_response_body(monkeypatch, caplog):
    responses = [
        _FakeResponse(429, headers={"Retry-After": "3"}),
        _FakeResponse(200, payload={"jsonrpc": "2.0", "result": 123}),
    ]
    monkeypatch.setattr(rpc_client.aiohttp, "ClientSession", lambda **kwargs: _FakeSession(responses))
    limiter = _FakeLimiter()
    monkeypatch.setattr(rpc_client, "_limiter_for_url", lambda _url: limiter)
    delays = []
    monkeypatch.setattr(rpc_client, "_retry_delay", lambda attempt, retry_after: delays.append(retry_after) or 0)

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    caplog.set_level(logging.WARNING, logger="rpc_client")

    assert asyncio.run(rpc_client._try_endpoint(
        "https://rpc.example/?api-key=private", {"method": "getSlot"}, 3
    )) == 123
    assert delays == ["3"]
    assert limiter.delays == [0]
    assert "private" not in caplog.text
    assert "response body" not in caplog.text


def test_send_transaction_is_not_retried_after_429(monkeypatch):
    responses = [
        _FakeResponse(429, headers={"Retry-After": "2"}),
        _FakeResponse(200, payload={"jsonrpc": "2.0", "result": "unexpected"}),
    ]
    monkeypatch.setattr(rpc_client.random, "uniform", lambda low, high: 0)
    monkeypatch.setattr(rpc_client.aiohttp, "ClientSession", lambda **kwargs: _FakeSession(responses))
    limiter = _FakeLimiter()
    monkeypatch.setattr(rpc_client, "_limiter_for_url", lambda _url: limiter)

    async def no_sleep(_delay):
        raise AssertionError("transaction submission must not retry")

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    result = asyncio.run(rpc_client._try_endpoint(
        "https://rpc.example", {"method": "sendTransaction"}, 3, max_retries=3
    ))

    assert result is None
    assert len(responses) == 1
    assert limiter.delays == [2.0]


def test_long_retry_after_skips_the_rate_limited_endpoint(monkeypatch):
    responses = [
        _FakeResponse(429, headers={"Retry-After": "999"}),
        _FakeResponse(200, payload={"jsonrpc": "2.0", "result": 123}),
    ]
    monkeypatch.setattr(rpc_client.aiohttp, "ClientSession", lambda **kwargs: _FakeSession(responses))
    limiter = _FakeLimiter()
    monkeypatch.setattr(rpc_client, "_limiter_for_url", lambda _url: limiter)

    result = asyncio.run(rpc_client._try_endpoint(
        "https://rpc.example", {"method": "getSlot"}, 3
    ))

    assert result is None
    assert len(responses) == 1
    assert limiter.delays[0] >= 999


def test_send_transaction_does_not_fail_over_to_secondary_rpc(monkeypatch):
    calls = []

    async def fail_endpoint(url, payload, timeout, **kwargs):
        calls.append((url, payload["method"]))
        return None

    monkeypatch.setattr(rpc_client.config, "HELIUS_RPC_URL", "https://primary.example")
    monkeypatch.setattr(rpc_client.config, "ALCHEMY_RPC_URL", "https://secondary.example")
    monkeypatch.setattr(rpc_client, "_try_endpoint", fail_endpoint)

    result = asyncio.run(rpc_client.rpc_post({"method": "sendTransaction", "params": ["signed"]}))

    assert result == {}
    assert calls == [("https://primary.example", "sendTransaction")]


def test_unknown_rpc_method_is_not_retried_or_failed_over(monkeypatch):
    calls = []

    async def try_endpoint(url, payload, timeout, **kwargs):
        calls.append((url, payload["method"], kwargs.get("max_retries")))
        return None

    monkeypatch.setattr(rpc_client.config, "HELIUS_RPC_URL", "https://primary.example")
    monkeypatch.setattr(rpc_client.config, "ALCHEMY_RPC_URL", "https://secondary.example")
    monkeypatch.setattr(rpc_client, "_try_endpoint", try_endpoint)

    result = asyncio.run(rpc_client.rpc_post({"method": "requestAirdrop"}))

    assert result == {}
    assert calls == [("https://primary.example", "requestAirdrop", 1)]


def test_read_only_request_still_fails_over_to_secondary_rpc(monkeypatch):
    calls = []

    async def try_endpoint(url, payload, timeout, **kwargs):
        calls.append(url)
        if url == "https://primary.example":
            return None
        return {"slot": 123}

    monkeypatch.setattr(rpc_client.config, "HELIUS_RPC_URL", "https://primary.example")
    monkeypatch.setattr(rpc_client.config, "ALCHEMY_RPC_URL", "https://secondary.example")
    monkeypatch.setattr(rpc_client, "_try_endpoint", try_endpoint)

    result = asyncio.run(rpc_client.rpc_post({"method": "getSlot"}))

    assert result == {"slot": 123}
    assert calls == ["https://primary.example", "https://secondary.example"]


def test_json_rpc_rate_limit_activates_shared_cooldown(monkeypatch, tmp_path):
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    responses = [
        _FakeResponse(200, payload={"jsonrpc": "2.0", "error": {
            "code": -32005, "message": "Too many requests"
        }}),
        _FakeResponse(200, payload={"jsonrpc": "2.0", "result": 123}),
    ]
    monkeypatch.setattr(rpc_client.aiohttp, "ClientSession", lambda **kwargs: _FakeSession(responses))
    limiter = _FakeLimiter()
    monkeypatch.setattr(rpc_client, "_limiter_for_url", lambda _url: limiter)
    monkeypatch.setattr(rpc_client, "_retry_delay", lambda _attempt, retry_after=None: 0.5)

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    before = rpc_client.get_metrics()["rate_limited"]

    result = asyncio.run(rpc_client._try_endpoint(
        "https://rpc.example", {"method": "getSlot"}, 3, max_retries=2
    ))

    assert result == 123
    assert limiter.delays == [0.5]
    assert rpc_client.get_metrics()["rate_limited"] == before + 1
