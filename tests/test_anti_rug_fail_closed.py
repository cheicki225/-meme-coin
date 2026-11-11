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
