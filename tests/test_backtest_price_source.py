import asyncio
import time

import pytest

import backtest
import config


class _FakeResponse:
    def __init__(self, data=None, status=200):
        self._data = data
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self):
        return self._data


class _FakeSession:
    def __init__(self, data=None, status=200):
        self._data = data
        self._status = status
        self.request_options = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def get(self, _url, **kwargs):
        self.request_options = kwargs
        return _FakeResponse(self._data, self._status)


def test_quote_uses_sol_as_base_and_returns_source_and_receive_time(monkeypatch):
    session = _FakeSession({
        "pairs": [
            {
                "baseToken": {"address": "unrelated"},
                "priceUsd": "999999",
                "liquidity": {"usd": 1_000_000},
            },
            {
                "baseToken": {"address": config.SOL_MINT},
                "priceUsd": "123.45",
                "liquidity": {"usd": 20_000},
            },
        ],
    })
    monkeypatch.setattr(backtest.rpc_client, "get_http_connector", lambda: None)
    monkeypatch.setattr(backtest.aiohttp, "ClientSession", lambda **_kwargs: session)
    monkeypatch.setattr(backtest, "_sol_price_cache", {"quote": None, "monotonic_ts": 0.0})

    quote = asyncio.run(backtest.get_sol_usd_quote())

    assert quote.rate == 123.45
    assert quote.source == "dexscreener_https"
    assert quote.received_at_utc.endswith("+00:00")
    assert session.request_options["allow_redirects"] is False


def test_rate_rejects_expired_cache_and_static_fallback_when_provider_fails(monkeypatch):
    stale_quote = backtest.SolUsdQuote(150.0, "old_fixture", "2020-01-01T00:00:00+00:00")
    monkeypatch.setattr(
        backtest,
        "_sol_price_cache",
        {"quote": stale_quote, "monotonic_ts": time.monotonic() - backtest.SOL_PRICE_CACHE_TTL_S - 1},
    )
    monkeypatch.setattr(config, "SOL_USD_RATE", 999_999.0)
    monkeypatch.setattr(backtest.rpc_client, "get_http_connector", lambda: None)

    def fail_session(**_kwargs):
        raise OSError("synthetic transport failure")

    monkeypatch.setattr(backtest.aiohttp, "ClientSession", fail_session)
    with pytest.raises(backtest.SolUsdPriceUnavailable):
        asyncio.run(backtest.get_sol_usd_rate())


def test_rate_rejects_invalid_provider_response_without_using_untrusted_pair(monkeypatch):
    session = _FakeSession({"pairs": [{
        "baseToken": {"address": config.SOL_MINT},
        "priceUsd": "NaN",
        "liquidity": {"usd": 10_000},
    }]})
    monkeypatch.setattr(backtest.rpc_client, "get_http_connector", lambda: None)
    monkeypatch.setattr(backtest.aiohttp, "ClientSession", lambda **_kwargs: session)
    monkeypatch.setattr(backtest, "_sol_price_cache", {"quote": None, "monotonic_ts": 0.0})

    with pytest.raises(backtest.SolUsdPriceUnavailable, match="no_valid_sol_base_pair"):
        asyncio.run(backtest.get_sol_usd_quote())
