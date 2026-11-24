"""Redirect handling tests for credential-bearing RPC endpoints."""

from __future__ import annotations

import asyncio

import rpc_client


class _FakeResponse:
    def __init__(self, status: int, *, payload=None, headers=None):
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
    def __init__(self, response: _FakeResponse):
        self.response = response
        self.post_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        return self.response


class _FakeLimiter:
    async def wait_if_needed(self) -> bool:
        return True

    def defer(self, _seconds: float) -> None:
        return None


def test_parsed_transaction_request_disables_redirects(monkeypatch, tmp_path):
    session = _FakeSession(_FakeResponse(200, payload=[{"signature": "parsed"}]))
    monkeypatch.setattr(rpc_client.aiohttp, "ClientSession", lambda **_kwargs: session)
    monkeypatch.setattr(rpc_client, "get_http_connector", lambda: None)
    monkeypatch.setattr(rpc_client, "wait_for_provider_slot", _allow_provider_slot)
    monkeypatch.setattr(rpc_client.config, "HELIUS_API_KEY", "test-key")
    monkeypatch.setattr(
        rpc_client.config,
        "HELIUS_PARSE_TX_URL",
        "https://helius.example/parse?api-key=test-key",
    )
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(tmp_path / "metrics.jsonl"))

    result = asyncio.run(rpc_client.fetch_parsed_transaction("test-signature"))

    assert result == {"signature": "parsed"}
    assert len(session.post_calls) == 1
    assert session.post_calls[0][1]["allow_redirects"] is False


async def _allow_provider_slot(_url: str) -> bool:
    return True


def test_rpc_endpoint_request_disables_redirects(monkeypatch, tmp_path):
    session = _FakeSession(
        _FakeResponse(200, payload={"jsonrpc": "2.0", "result": 123})
    )
    monkeypatch.setattr(rpc_client.aiohttp, "ClientSession", lambda **_kwargs: session)
    monkeypatch.setattr(rpc_client, "get_http_connector", lambda: None)
    monkeypatch.setattr(rpc_client, "_limiter_for_url", lambda _url: _FakeLimiter())
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(tmp_path / "metrics.jsonl"))

    result = asyncio.run(
        rpc_client._try_endpoint(
            "https://rpc.example/?api-key=test-key",
            {"method": "getSlot"},
            3,
        )
    )

    assert result == 123
    assert len(session.post_calls) == 1
    assert session.post_calls[0][1]["allow_redirects"] is False
