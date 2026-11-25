import asyncio
import ast
from pathlib import Path

import ai_advisor
import debug_solscan_auth
import find_dev
import gmgn_client
import mobula_client
import security
import solscan_client
import websocket_listener


class _RedirectResponse:
    status = 302
    headers = {"Location": "https://redirect.invalid/collect"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self):
        raise AssertionError("redirect response must not be followed")

    async def text(self):
        return "redirect refused"


class _Session:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return _RedirectResponse()

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return _RedirectResponse()


def _patch_session(monkeypatch, module, session):
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda **_kwargs: session)
    monkeypatch.setattr(module.rpc_client, "get_http_connector", lambda: None)


def test_ai_provider_authorization_headers_do_not_follow_redirects(monkeypatch):
    session = _Session()
    _patch_session(monkeypatch, ai_advisor, session)
    monkeypatch.setattr(ai_advisor.config, "ANTHROPIC_API_KEY", "test-only-anthropic")
    monkeypatch.setattr(ai_advisor.config, "GROK_API_KEY", "test-only-grok")

    assert asyncio.run(ai_advisor._call_anthropic("fixture prompt")) is None
    assert asyncio.run(ai_advisor._call_grok("fixture prompt")) is None
    assert [call[2]["allow_redirects"] for call in session.calls] == [False, False]


def test_wallet_provider_auth_headers_do_not_follow_redirects(monkeypatch):
    session = _Session()
    _patch_session(monkeypatch, gmgn_client, session)
    _patch_session(monkeypatch, mobula_client, session)
    monkeypatch.setattr(gmgn_client.config, "GMGN_API_KEY", "test-only-gmgn")
    monkeypatch.setattr(mobula_client.config, "MOBULA_API_KEY", "test-only-mobula")

    assert asyncio.run(gmgn_client.get_wallet_stats("fixture-wallet")) == {}
    assert asyncio.run(gmgn_client.get_wallet_profits("fixture-wallet")) == {}
    assert asyncio.run(mobula_client.get_wallet_labels("fixture-wallet")) is None
    assert asyncio.run(mobula_client.get_deployer_tokens("fixture-wallet")) == []
    assert len(session.calls) == 4
    assert all(call[2]["allow_redirects"] is False for call in session.calls)


def test_security_and_solscan_auth_headers_do_not_follow_redirects(monkeypatch):
    session = _Session()
    _patch_session(monkeypatch, security, session)
    _patch_session(monkeypatch, solscan_client, session)
    monkeypatch.setattr(security.config, "GOPLUS_API_KEY", "test-only-goplus")
    monkeypatch.setattr(security.config, "GOPLUS_API_SECRET", "test-only-goplus-secret")
    monkeypatch.setattr(solscan_client.config, "SOLSCAN_API_KEY", "test-only-solscan")

    result = asyncio.run(security.check_token_security("fixture-mint"))
    transfers = asyncio.run(solscan_client.find_fixed_amount_transfers("fixture-wallet", 1.0))

    assert result["honeypot_detection_available"] is False
    assert transfers == []
    assert len(session.calls) == 2
    assert all(call[2]["allow_redirects"] is False for call in session.calls)


def test_helius_parse_urls_with_query_credentials_do_not_follow_redirects(monkeypatch):
    session = _Session()
    _patch_session(monkeypatch, websocket_listener, session)
    monkeypatch.setattr(websocket_listener.config, "HELIUS_API_KEY", "test-only-helius")
    monkeypatch.setattr(
        websocket_listener.config,
        "HELIUS_PARSE_TX_URL",
        "https://api.helius.invalid/parse?api-key=test-only-helius",
    )

    async def available(_url):
        return True

    monkeypatch.setattr(websocket_listener.rpc_client, "wait_for_provider_slot", available)
    listener = object.__new__(websocket_listener.NewTokenListener)
    assert asyncio.run(listener._fetch_parsed_transaction("fixture-signature")) == {}
    assert session.calls[0][2]["allow_redirects"] is False

    find_session = _Session()
    _patch_session(monkeypatch, find_dev, find_session)
    monkeypatch.setattr(find_dev.config, "HELIUS_API_KEY", "test-only-helius")
    monkeypatch.setattr(
        find_dev.config,
        "HELIUS_PARSE_TX_URL",
        "https://api.helius.invalid/parse?api-key=test-only-helius",
    )

    async def available_for_find_dev(_url):
        return True

    monkeypatch.setattr(
        find_dev.rpc_client, "wait_for_provider_slot", available_for_find_dev
    )
    async def no_wait_sleep(_delay):
        return None

    monkeypatch.setattr(find_dev.asyncio, "sleep", no_wait_sleep)
    assert asyncio.run(find_dev._fetch_parsed_transaction("fixture-signature")) == {}
    assert find_session.calls
    assert all(call[2]["allow_redirects"] is False for call in find_session.calls)


def test_solscan_diagnostic_never_redirects_or_prints_test_key(capsys):
    session = _Session()

    asyncio.run(
        debug_solscan_auth.try_variant(
            session, "fixture", {"token": "test-only-solscan-key"}, {}
        )
    )

    assert session.calls[0][2]["allow_redirects"] is False
    assert "test-only-solscan-key" not in capsys.readouterr().out


def test_helius_debug_cli_refuses_redirects_and_does_not_print_body():
    path = Path(__file__).resolve().parents[1] / "debug_helius_transfers.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    posts = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "post"
    ]
    assert len(posts) == 1
    allow_redirects = next(
        keyword.value for keyword in posts[0].keywords
        if keyword.arg == "allow_redirects"
    )
    assert isinstance(allow_redirects, ast.Constant) and allow_redirects.value is False
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "text"
        for node in ast.walk(tree)
    )
