import asyncio
from types import SimpleNamespace

import pytest

import security


TOKEN_MINT = "AbCdEf123"


def _control(status="0", authorities=None, field="authority"):
    return {"status": status, field: [] if authorities is None else authorities}


def _response(**updates):
    token = {
        "mintable": _control(),
        "freezable": _control(),
        "closable": _control(),
        "metadata_mutable": _control(field="metadata_upgrade_authority"),
        "default_account_state": "1",
        "non_transferable": "0",
        "transfer_fee": {},
        "transfer_hook": [],
        "dex": [{"dex_name": "raydium", "type": "Standard", "tvl": "25000.50"}],
        "lp_holders": [{"is_locked": 1, "percent": "1"}],
    }
    token.update(updates)
    return {"code": 1, "message": "ok", "result": {TOKEN_MINT: token}}


def test_parses_documented_solana_schema_without_claiming_evm_honeypot_safety():
    result = security._parse_token_security(_response(), TOKEN_MINT)

    assert result["error"] is None
    assert result["risk_model"] == "goplus_solana_authorities_v1"
    assert result["security_score"] == 100
    assert result["is_honeypot"] is None
    assert result["honeypot_detection_available"] is False
    assert result["buy_tax"] is None
    assert result["sell_tax"] is None
    assert result["is_mintable"] is False
    assert result["is_freezable"] is False
    assert result["is_closable"] is False
    assert result["max_dex_tvl_usd"] == 25000.5
    assert result["lp_locked_pct"] == 100
    assert result["lp_lock_data_available"] is True


def test_empty_lp_holder_list_is_unknown_and_score_blocking():
    result = security._parse_token_security(
        _response(lp_holders=[]), TOKEN_MINT
    )

    assert result["error"] is None
    assert result["lp_locked_pct"] is None
    assert result["lp_lock_data_available"] is False
    assert result["security_score"] == 0
    report = security.format_security_report(TOKEN_MINT, result)
    assert "inconnu / indisponible" in report
    assert "Le filtre refuse l'achat si ces données sont inconnues." in report


@pytest.mark.parametrize(
    "holders,expected_pct,expected_penalty,expected_score",
    [
        ([{"is_locked": 0, "percent": "1"}], 0, 45, 55),
        ([{"is_locked": 1, "percent": "0.4"}, {"is_locked": 0, "percent": "0.6"}], 40, 30, 70),
        ([{"is_locked": 1, "percent": "0.85"}, {"is_locked": 0, "percent": "0.15"}], 85, 0, 100),
    ],
)
def test_lp_locking_sample_applies_documented_score_penalty(
    holders, expected_pct, expected_penalty, expected_score
):
    result = security._parse_token_security(
        _response(lp_holders=holders), TOKEN_MINT
    )

    assert result["error"] is None
    assert result["lp_locked_pct"] == expected_pct
    assert result["lp_lock_penalty"] == expected_penalty
    assert result["security_score"] == expected_score

    report = security.format_security_report(TOKEN_MINT, result)
    assert "couverture API non exhaustive" in report
    if expected_pct == 0:
        assert "Pénalité appliquée au score pour le verrouillage LP: -45 points" in report


def test_inconsistent_lp_holder_percentages_are_unknown_and_blocking():
    result = security._parse_token_security(
        _response(lp_holders=[
            {"is_locked": 1, "percent": "1"},
            {"is_locked": 1, "percent": "1"},
        ]),
        TOKEN_MINT,
    )

    assert result["error"] == "inconsistent_lp_holder_percentage_total"
    assert result["security_score"] == 0


@pytest.mark.parametrize(
    "updates,expected_score",
    [
        ({"mintable": _control("1", [{"address": "mint-authority", "malicious_address": 0}])}, 50),
        ({"freezable": _control("1", [{"address": "freeze-authority", "malicious_address": 0}])}, 50),
        ({"closable": _control("1", [{"address": "close-authority", "malicious_address": 0}])}, 65),
        ({"metadata_mutable": _control("1", [{"address": "update-authority", "malicious_address": 0}], "metadata_upgrade_authority")}, 80),
        ({"transfer_hook": [{"address": "hook-program", "malicious_address": 0}]}, 55),
        ({"transfer_fee": {"current_fee_rate": {"fee_rate": 500}}}, 55),
    ],
)
def test_scores_active_solana_controls_and_token_2022_risks(updates, expected_score):
    result = security._parse_token_security(_response(**updates), TOKEN_MINT)

    assert result["error"] is None
    assert result["security_score"] == expected_score


def test_100_percent_transfer_fee_is_a_zero_score_hard_risk():
    result = security._parse_token_security(
        _response(transfer_fee={"current_fee_rate": {"fee_rate": 10_000}}), TOKEN_MINT
    )

    assert result["error"] is None
    assert result["security_score"] == 0
    assert result["transfer_fee_bps"] == 10_000


@pytest.mark.parametrize(
    "updates,expected_error",
    [
        ({"mintable": None}, "missing_or_invalid_mintable"),
        ({"freezable": {"status": "0", "authority": None}}, "missing_or_invalid_freezable.authority"),
        ({"default_account_state": None}, "missing_or_invalid_default_account_state"),
        ({"non_transferable": None}, "missing_or_invalid_non_transferable"),
        ({"transfer_fee": {"current_fee_rate": {"fee_rate": "NaN"}}}, "invalid_transfer_fee.current_fee_rate"),
        ({"transfer_fee": {"unknown": "data"}}, "unknown_transfer_fee"),
        ({"transfer_hook": None}, "missing_or_invalid_transfer_hook"),
        ({"dex": []}, "no_dex_liquidity"),
        ({"dex": [{"tvl": "Infinity"}]}, "invalid_dex.tvl"),
        ({"lp_holders": None}, "missing_or_invalid_lp_holders"),
    ],
)
def test_missing_or_malformed_solana_risk_data_is_unknown_and_blocking(updates, expected_error):
    result = security._parse_token_security(_response(**updates), TOKEN_MINT)

    assert result["error"] == expected_error
    assert result["security_score"] == 0


def test_frozen_or_nontransferable_token_is_blocked():
    frozen = security._parse_token_security(
        _response(default_account_state="2"), TOKEN_MINT
    )
    nontransferable = security._parse_token_security(
        _response(non_transferable="1"), TOKEN_MINT
    )

    assert frozen["error"] is None and frozen["security_score"] == 0
    assert nontransferable["error"] is None and nontransferable["security_score"] == 0


def test_malicious_authority_or_hook_is_blocking():
    malicious_authority = security._parse_token_security(
        _response(mintable=_control("1", [{"address": "bad", "malicious_address": 1}])), TOKEN_MINT
    )
    malicious_hook = security._parse_token_security(
        _response(transfer_hook=[{"address": "bad-hook", "malicious_address": 1}]), TOKEN_MINT
    )

    assert malicious_authority["error"] is None and malicious_authority["security_score"] == 0
    assert malicious_hook["error"] is None and malicious_hook["security_score"] == 0


def test_provider_error_and_missing_token_are_blocking():
    provider_error = security._parse_token_security({"code": 0, "result": {}}, TOKEN_MINT)
    missing_token = security._parse_token_security(
        {"code": 1, "result": {}}, TOKEN_MINT
    )

    assert provider_error["error"] == "provider_error"
    assert missing_token["error"] == "not_indexed"
    assert provider_error["security_score"] == missing_token["security_score"] == 0


def test_security_report_never_shows_green_for_unknown_data():
    report = security.format_security_report(
        TOKEN_MINT, {"error": "not_indexed", "security_score": 0}
    )
    partial = security._parse_token_security(_response(), TOKEN_MINT)
    partial["lp_locked_pct"] = None
    partial["lp_lock_data_available"] = False
    partial_report = security.format_security_report(TOKEN_MINT, partial)

    assert "inconnu / indisponible" in report
    assert "🟢" not in report
    assert "Faible" not in report
    assert "inconnu / indisponible" in partial_report
    assert "🟢" not in partial_report


def test_security_report_displays_solana_risks_and_does_not_invent_evm_fields():
    result = security._parse_token_security(
        _response(
            mintable=_control("1", [{"address": "mint-authority", "malicious_address": 0}]),
            transfer_fee={"current_fee_rate": {"fee_rate": 250}},
            transfer_hook=[{"address": "hook-program", "malicious_address": 0}],
        ),
        TOKEN_MINT,
    )

    report = security.format_security_report(TOKEN_MINT, result)

    assert "Autorité de mint active" in report
    assert "Frais de transfert: 250 points de base" in report
    assert "Transfer hook présent" in report
    assert "Taxe buy/sell et détection honeypot EVM: non fournies par ce schéma Solana." in report
    assert "🟢" not in report


def test_security_result_requires_complete_solana_contract():
    parsed = security._parse_token_security(_response(), TOKEN_MINT)

    assert security.is_valid_solana_security_result(parsed) is True
    assert security.is_valid_solana_security_result({"error": None, "security_score": 80}) is False
    assert security.is_valid_solana_security_result({**parsed, "lp_lock_data_available": False}) is False
    assert security.is_valid_solana_security_result({**parsed, "max_dex_tvl_usd": 0}) is False


def test_public_solana_endpoint_is_called_without_auth_headers_when_no_key(monkeypatch):
    captured = {}

    class _Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def json(self):
            return _response()

    class _Session:
        def __init__(self, **kwargs):
            captured["session_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, url, **kwargs):
            captured["url"] = url
            captured["request_kwargs"] = kwargs
            return _Response()

    monkeypatch.setattr(security.config, "GOPLUS_API_KEY", "")
    monkeypatch.setattr(security.config, "GOPLUS_API_SECRET", "")
    monkeypatch.setattr(security.rpc_client, "get_http_connector", lambda: None)
    monkeypatch.setattr(security.aiohttp, "ClientSession", _Session)

    result = asyncio.run(security.check_token_security(TOKEN_MINT))

    assert result["error"] is None
    assert "token_security" in captured["url"]
    headers = captured["request_kwargs"]["headers"]
    assert "x-api-key" not in headers
    assert "Authorization" not in headers
