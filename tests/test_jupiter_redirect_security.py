import asyncio

import pytest

import config
import jupiter_executor


class _Response:
    def __init__(self, data, status=200):
        self.data = data
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return self.data


class _Session:
    calls = []
    get_response = _Response({"requestId": "test-request"})
    post_response = _Response({"signature": "test-signature", "status": "Success"})

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.get_response

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.post_response


def test_jupiter_api_key_and_signed_transaction_requests_do_not_follow_redirects(monkeypatch, tmp_path):
    _Session.calls = []
    monkeypatch.setattr(jupiter_executor.aiohttp, "ClientSession", _Session)
    monkeypatch.setattr(config, "JUPITER_API_KEY", "test-key")
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(tmp_path / "metrics.jsonl"))

    asyncio.run(jupiter_executor.get_quote(
        config.SOL_MINT, "TokenMint", 100, 100, user_pubkey="TestWallet",
    ))
    asyncio.run(jupiter_executor._execute_order(
        {"requestId": "test-request", "lastValidBlockHeight": 123},
        b"signed-test-bytes", "test-signature",
    ))

    assert [call[0] for call in _Session.calls] == ["GET", "POST"]
    assert all(call[2]["allow_redirects"] is False for call in _Session.calls)


@pytest.mark.parametrize("method", ["get", "post"])
def test_jupiter_redirect_responses_fail_closed(monkeypatch, tmp_path, method):
    _Session.calls = []
    monkeypatch.setattr(jupiter_executor.aiohttp, "ClientSession", _Session)
    monkeypatch.setattr(config, "JUPITER_API_KEY", "test-key")
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(tmp_path / f"{method}.jsonl"))
    if method == "get":
        _Session.get_response = _Response({}, status=302)
        call = jupiter_executor.get_quote(
            config.SOL_MINT, "TokenMint", 100, 100, user_pubkey="TestWallet",
        )
    else:
        _Session.post_response = _Response({}, status=302)
        call = jupiter_executor._execute_order(
            {"requestId": "test-request", "lastValidBlockHeight": 123}, b"signed", "test-signature",
        )

    with pytest.raises(jupiter_executor.SwapError, match="HTTP 302"):
        asyncio.run(call)
    assert len(_Session.calls) == 1
    assert _Session.calls[0][2]["allow_redirects"] is False
