import asyncio
import logging

import rpc_client


def test_retry_delay_uses_exponential_backoff_and_retry_after(monkeypatch):
    monkeypatch.setattr(rpc_client.random, "uniform", lambda low, high: 0)
    assert rpc_client._retry_delay(0) == 0.5
    assert rpc_client._retry_delay(1) == 1.0
    assert rpc_client._retry_delay(0, "2") == 2
    assert rpc_client._retry_delay(0, "999") == 30


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


def test_rpc_retries_429_and_does_not_log_response_body(monkeypatch, caplog):
    responses = [
        _FakeResponse(429, headers={"Retry-After": "3"}),
        _FakeResponse(200, payload={"jsonrpc": "2.0", "result": "signature"}),
    ]
    monkeypatch.setattr(rpc_client.aiohttp, "ClientSession", lambda **kwargs: _FakeSession(responses))

    async def no_wait():
        return None

    monkeypatch.setattr(rpc_client._rate_limiter, "wait_if_needed", no_wait)
    delays = []
    monkeypatch.setattr(rpc_client, "_retry_delay", lambda attempt, retry_after: delays.append(retry_after) or 0)

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    caplog.set_level(logging.WARNING, logger="rpc_client")

    assert asyncio.run(rpc_client._try_endpoint(
        "https://rpc.example/?api-key=private", {"method": "sendTransaction"}, 3
    )) == "signature"
    assert delays == ["3"]
    assert "private" not in caplog.text
    assert "response body" not in caplog.text
