import asyncio

import security


def test_goplus_missing_credentials_is_fail_closed(monkeypatch):
    monkeypatch.setattr(security.config, "GOPLUS_API_KEY", "")
    monkeypatch.setattr(security.config, "GOPLUS_API_SECRET", "")
    result = asyncio.run(security.check_token_security("Mint"))
    assert result["error"] == "no_api_key"
    assert result["security_score"] == 0
    assert result["is_honeypot"] is None


def test_security_numeric_parser_rejects_non_finite_values():
    assert security._safe_float("nan", 17) == 17
    assert security._safe_float("inf", 17) == 17
    assert security._safe_float("12.5", 17) == 12.5



def test_goplus_incomplete_security_fields_are_not_scored(monkeypatch):
    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self):
            return {
                "result": {
                    "mint": {
                        "is_honeypot": "0",
                        "buy_tax": "0",
                        "sell_tax": "0",
                        "is_mintable": "0",
                    }
                }
            }

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(security.config, "GOPLUS_API_KEY", "test-key")
    monkeypatch.setattr(security.config, "GOPLUS_API_SECRET", "")
    monkeypatch.setattr(security.aiohttp, "ClientSession", lambda *args, **kwargs: Session())

    result = asyncio.run(security.check_token_security("Mint"))
    assert result["error"] == "incomplete_security_data"
    assert result["security_score"] == 0



def test_goplus_sol_schema_scores_authority_and_transfer_risks(monkeypatch):
    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self):
            return {
                "result": {
                    "Mint": {
                        "mintable": {"status": "1"},
                        "freezable": {"status": "0"},
                        "closable": {"status": "0"},
                        "default_account_state": "1",
                        "non_transferable": "0",
                        "balance_mutable_authority": {"status": "0"},
                        "transfer_fee": {"current_fee_rate": {"fee_rate": "200"}},
                        "creator": [{"malicious_address": "0"}],
                        "transfer_hook": {"malicious_address": "0"},
                        "dex": [{"tvl": "50000"}],
                    }
                }
            }

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(security.config, "GOPLUS_API_KEY", "test-token")
    monkeypatch.setattr(security.config, "GOPLUS_API_SECRET", "")
    monkeypatch.setattr(security.aiohttp, "ClientSession", lambda *args, **kwargs: Session())

    result = asyncio.run(security.check_token_security("Mint"))
    assert result["error"] is None
    assert result["is_mintable"] is True
    assert result["security_score"] == 80
    assert result["transfer_fee_pct"] == 2.0
    assert result["dex_liquidity_usd"] == 50000.0
