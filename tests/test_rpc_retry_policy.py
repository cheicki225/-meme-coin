import asyncio
import logging

import pytest

import config
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
        self.post_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        return self.responses.pop(0)


def test_idempotent_rpc_retries_429_and_does_not_log_response_body(monkeypatch, caplog):
    responses = [
        _FakeResponse(429, headers={"Retry-After": "3"}),
        _FakeResponse(200, payload={"jsonrpc": "2.0", "result": "slot-value"}),
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
        "https://rpc.example/?api-key=private", {"method": "getSlot"}, 3
    )) == "slot-value"
    assert delays == ["3"]
    assert "private" not in caplog.text
    assert "response body" not in caplog.text


def test_rpc_post_rejects_transaction_submissions_without_signature(monkeypatch):
    calls = []

    async def unexpected_call(*args, **kwargs):
        calls.append((args, kwargs))
        return "should-not-run"

    monkeypatch.setattr(rpc_client, "_try_endpoint", unexpected_call)
    with pytest.raises(ValueError, match="submit_transaction"):
        asyncio.run(rpc_client.rpc_post({"method": "sendTransaction"}))
    assert calls == []


def test_send_transaction_429_is_one_primary_attempt_without_fallback(
    monkeypatch, caplog, tmp_path
):
    signature = "preserved-signature"
    responses = [
        _FakeResponse(429, headers={"Retry-After": "3"}),
        _FakeResponse(200, payload={"jsonrpc": "2.0", "result": signature}),
    ]
    session = _FakeSession(responses)
    monkeypatch.setattr(rpc_client.aiohttp, "ClientSession", lambda **kwargs: session)
    monkeypatch.setattr(rpc_client, "get_http_connector", lambda: None)
    monkeypatch.setattr(config, "HELIUS_RPC_URL", "https://primary.example/rpc")
    monkeypatch.setattr(config, "ALCHEMY_RPC_URL", "https://fallback.example/rpc")
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(tmp_path / "metrics.jsonl"))

    async def no_wait():
        return None

    monkeypatch.setattr(rpc_client._rate_limiter, "wait_if_needed", no_wait)
    caplog.set_level(logging.WARNING, logger="rpc_client")

    payload = {"jsonrpc": "2.0", "method": "sendTransaction", "params": ["signed-bytes"]}
    with pytest.raises(rpc_client.RpcSubmissionOutcomeUnknown) as caught:
        asyncio.run(rpc_client.submit_transaction(payload, signature, timeout=3))

    assert caught.value.signature == signature
    assert len(session.post_calls) == 1
    assert session.post_calls[0][0][0] == "https://primary.example/rpc"
    assert session.post_calls[0][1]["json"] == payload
    assert len(responses) == 1
    assert signature not in caplog.text


def test_send_transaction_timeout_is_not_retried_or_failed_over(monkeypatch, tmp_path):
    class _TimeoutSession(_FakeSession):
        def post(self, *args, **kwargs):
            self.post_calls.append((args, kwargs))
            raise asyncio.TimeoutError

    session = _TimeoutSession([])
    monkeypatch.setattr(rpc_client.aiohttp, "ClientSession", lambda **kwargs: session)
    monkeypatch.setattr(rpc_client, "get_http_connector", lambda: None)
    monkeypatch.setattr(config, "HELIUS_RPC_URL", "https://primary.example/rpc")
    monkeypatch.setattr(config, "ALCHEMY_RPC_URL", "https://fallback.example/rpc")
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(tmp_path / "metrics.jsonl"))

    async def no_wait():
        return None

    monkeypatch.setattr(rpc_client._rate_limiter, "wait_if_needed", no_wait)
    with pytest.raises(rpc_client.RpcSubmissionOutcomeUnknown) as caught:
        asyncio.run(rpc_client.submit_transaction(
            {"method": "sendTransaction", "params": ["signed-bytes"]},
            "timeout-signature",
        ))

    assert caught.value.signature == "timeout-signature"
    assert len(session.post_calls) == 1
    assert session.post_calls[0][0][0] == "https://primary.example/rpc"


def test_send_transaction_success_returns_the_same_signature(monkeypatch, tmp_path):
    signature = "confirmed-request-signature"
    responses = [_FakeResponse(200, payload={"jsonrpc": "2.0", "result": signature})]
    session = _FakeSession(responses)
    monkeypatch.setattr(rpc_client.aiohttp, "ClientSession", lambda **kwargs: session)
    monkeypatch.setattr(rpc_client, "get_http_connector", lambda: None)
    monkeypatch.setattr(config, "HELIUS_RPC_URL", "https://primary.example/rpc")
    monkeypatch.setattr(config, "ALCHEMY_RPC_URL", "https://fallback.example/rpc")
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(tmp_path / "metrics.jsonl"))

    async def no_wait():
        return None

    monkeypatch.setattr(rpc_client._rate_limiter, "wait_if_needed", no_wait)
    result = asyncio.run(rpc_client.submit_transaction(
        {"method": "sendTransaction", "params": ["signed-bytes"]}, signature
    ))

    assert result == signature
    assert len(session.post_calls) == 1
    assert len(responses) == 0
