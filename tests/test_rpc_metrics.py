import asyncio

import config
import rpc_client
from execution_metrics import summarize_metrics


def test_rpc_request_failure_is_measured_without_recording_method_or_endpoint(monkeypatch, tmp_path):
    metrics_path = tmp_path / "metrics.jsonl"
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(metrics_path))
    monkeypatch.setattr(config, "HELIUS_RPC_URL", "https://rpc.invalid/private-path-placeholder")
    monkeypatch.setattr(config, "ALCHEMY_RPC_URL", "")

    async def failed_endpoint(*_args, **_kwargs):
        return None

    monkeypatch.setattr(rpc_client, "_try_endpoint", failed_endpoint)
    assert asyncio.run(rpc_client.rpc_post({"method": "getSlot"})) == {}

    summary = summarize_metrics(metrics_path)
    assert summary["stages"]["rpc.request:rpc_primary"]["failure_rate"] == 1
    content = metrics_path.read_text(encoding="utf-8")
    assert "private-path-placeholder" not in content
    assert "getSlot" not in content


def test_rpc_metrics_distinguish_primary_and_fallback_path(monkeypatch, tmp_path):
    metrics_path = tmp_path / "metrics.jsonl"
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(metrics_path))
    monkeypatch.setattr(config, "HELIUS_RPC_URL", "https://primary.invalid/private-path-placeholder")
    monkeypatch.setattr(config, "ALCHEMY_RPC_URL", "https://fallback.invalid/private-path-placeholder")
    calls = 0

    async def fail_primary(url, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        return None if "primary" in url else {"slot": 123}

    monkeypatch.setattr(rpc_client, "_try_endpoint", fail_primary)
    assert asyncio.run(rpc_client.rpc_post({"method": "getSlot"})) == {"slot": 123}

    summary = summarize_metrics(metrics_path)
    assert summary["stages"]["rpc.request:rpc_fallback"]["failure_rate"] == 0
    assert calls == 2
    assert "private-path-placeholder" not in metrics_path.read_text(encoding="utf-8")


def test_rpc_metrics_record_each_primary_retry_separately(monkeypatch, tmp_path):
    metrics_path = tmp_path / "attempts.jsonl"
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(metrics_path))
    responses = [
        {"status": 429, "headers": {"Retry-After": "0"}, "body": {}},
        {"status": 200, "headers": {}, "body": {"jsonrpc": "2.0", "result": 42}},
    ]

    class Response:
        def __init__(self, response):
            self.status = response["status"]
            self.headers = response["headers"]
            self.body = response["body"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def json(self):
            return self.body

    class Session:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return Response(responses.pop(0))

    async def no_wait():
        return True

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(rpc_client.aiohttp, "ClientSession", Session)
    limiter = rpc_client._limiter_for_url("https://rpc.invalid/credential-redacted")
    monkeypatch.setattr(limiter, "wait_if_needed", no_wait)
    monkeypatch.setattr(rpc_client.asyncio, "sleep", no_sleep)

    result = asyncio.run(rpc_client._try_endpoint(
        "https://rpc.invalid/credential-redacted", {"method": "getSlot"}, 1,
        max_retries=2, provider="rpc_primary",
    ))

    assert result == 42
    summary = summarize_metrics(metrics_path)
    attempt = summary["stages"]["rpc.endpoint_attempt:rpc_primary"]
    assert attempt["count"] == 2
    assert attempt["failure_rate"] == 0.5
    assert len(responses) == 0
