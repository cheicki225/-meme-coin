import asyncio

import pytest

import rpc_client


def test_identical_inflight_reads_share_one_request(monkeypatch, tmp_path):
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    monkeypatch.setattr(rpc_client.config, "HELIUS_RPC_URL", "https://primary.example")
    monkeypatch.setattr(rpc_client.config, "ALCHEMY_RPC_URL", None)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fake_try_endpoint(url, payload, timeout, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"slot": 123}

    monkeypatch.setattr(rpc_client, "_try_endpoint", fake_try_endpoint)

    async def run_requests():
        payload = {
            "jsonrpc": "2.0",
            "method": "getSlot",
            "params": [{"commitment": "confirmed"}],
        }
        first = asyncio.create_task(rpc_client.rpc_post({**payload, "id": 1}))
        await started.wait()
        second = asyncio.create_task(rpc_client.rpc_post({**payload, "id": 2}))
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(first, second)

    assert asyncio.run(run_requests()) == [{"slot": 123}, {"slot": 123}]
    assert calls == 1
    assert rpc_client.get_metrics()["coalesced"] >= 1


def test_only_same_provider_host_shares_rate_limit_state():
    primary = rpc_client._limiter_for_url("https://rpc.example/v2/credential-placeholder")
    same_provider = rpc_client._limiter_for_url("https://rpc.example/other-path")
    fallback = rpc_client._limiter_for_url("https://fallback.example/v2/credential-placeholder")

    assert primary is same_provider
    assert primary is not fallback

    async def check_cooldown():
        rpc_client.defer_provider("https://rpc.example/v2/credential-placeholder", 60)
        assert not await same_provider.wait_if_needed()
        assert await fallback.wait_if_needed()

    asyncio.run(check_cooldown())


def test_helius_rpc_and_enhanced_api_use_separate_limiters():
    rpc = rpc_client._limiter_for_url("https://mainnet.helius-rpc.com/?api-key=credential")
    enhanced = rpc_client._limiter_for_url("https://api.helius.xyz/v0/transactions/?api-key=credential")

    assert rpc is not enhanced
    assert rpc._interval_seconds == pytest.approx(1 / rpc_client.config.SOLANA_RPC_REQUESTS_PER_SECOND)
    assert enhanced._interval_seconds == pytest.approx(
        1 / rpc_client.config.HELIUS_ENHANCED_REQUESTS_PER_SECOND
    )


def test_rate_limiter_supports_fractional_requests_per_second():
    limiter = rpc_client._RateLimiter(0.5)

    async def reserve_first_slot():
        started = asyncio.get_running_loop().time()
        assert await limiter.wait_if_needed()
        return limiter._next_request_at - started

    interval = asyncio.run(reserve_first_slot())

    assert 1.9 <= interval <= 2.1


def test_read_coalescing_key_includes_timeout():
    payload = {"jsonrpc": "2.0", "method": "getSlot", "params": []}

    async def keys_differ():
        return rpc_client._inflight_read_key(payload, 3) != rpc_client._inflight_read_key(payload, 12)

    assert asyncio.run(keys_differ())
