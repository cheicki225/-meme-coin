"""
GoPlus Solana Token Security adapter.

The Solana endpoint uses Solana-specific nested fields (mintable/freezable/
closable, transfer hooks, default account state, holders and DEX data). It does
not establish EVM-style ownership renunciation or honeypot fields. Unknown or
incomplete provider data is returned as an error and must be blocked by callers
when this check is enabled.
"""
from __future__ import annotations

import logging
import math

import aiohttp

import config
import rpc_client

log = logging.getLogger("security")


def _status(value):
    if isinstance(value, dict):
        value = value.get("status")
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true"}:
        return True
    if text in {"0", "false"}:
        return False
    return None


def _malicious(value):
    if isinstance(value, list):
        return any(_malicious(item) for item in value)
    if isinstance(value, dict):
        return str(value.get("malicious_address", "0")) == "1"
    return False


def _safe_float(value, default=None):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _error_result(reason: str) -> dict:
    return {
        "error": reason,
        "security_score": 0,
        "is_honeypot": None,
        "is_risky": True,
        "flags": {"verification_unavailable": True},
    }


async def check_token_security(token_mint: str) -> dict:
    """Return a conservative risk summary from GoPlus' Solana endpoint."""
    credential = config.GOPLUS_API_SECRET or config.GOPLUS_API_KEY
    if not credential:
        log.warning("GoPlus non configuré — vérification anti-rug indisponible.")
        return _error_result("no_api_key")

    url = (
        "https://api.gopluslabs.io/api/v1/solana/token_security"
        f"?contract_addresses={token_mint}"
    )
    headers = {"Accept": "application/json", "Authorization": f"Bearer {credential}"}
    try:
        async with aiohttp.ClientSession(
            connector=rpc_client.get_http_connector(), connector_owner=False
        ) as session:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status != 200:
                    log.warning("GoPlus HTTP %s pour %s", response.status, token_mint[:8])
                    return _error_result(f"http_{response.status}")
                payload = await response.json()
    except Exception as exc:
        log.warning("GoPlus indisponible pour %s: %s", token_mint[:8], exc)
        return _error_result("provider_unavailable")

    if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
        return _error_result("invalid_provider_response")
    result = payload["result"]
    token = result.get(token_mint, result.get(token_mint.lower()))
    if not isinstance(token, dict) or not token:
        return _error_result("not_indexed")

    # These core fields are needed to distinguish explicit "no" from unknown.
    required = ("mintable", "freezable", "closable", "default_account_state", "non_transferable")
    if any(key not in token or token.get(key) is None for key in required):
        return _error_result("incomplete_security_data")

    flags = {
        "mintable": _status(token.get("mintable")),
        "freezable": _status(token.get("freezable")),
        "closable": _status(token.get("closable")),
        "metadata_mutable": _status(token.get("metadata_mutable")),
        "balance_mutable": _status(token.get("balance_mutable_authority")),
        "transfer_fee_upgradable": _status(token.get("transfer_fee_upgradable")),
        "default_account_state_upgradable": _status(token.get("default_account_state_upgradable")),
        "transfer_hook_upgradable": _status(token.get("transfer_hook_upgradable")),
    }
    if any(flags[key] is None for key in ("mintable", "freezable", "closable")):
        return {"error": "incomplete_security_data", "security_score": 0, "is_honeypot": None}

    default_state = str(token.get("default_account_state"))
    non_transferable = str(token.get("non_transferable")) == "1"
    hook_malicious = _malicious(token.get("transfer_hook"))
    creator_malicious = _malicious(token.get("creator"))

    transfer_fee = token.get("transfer_fee") or {}
    current_fee = transfer_fee.get("current_fee_rate", {}) if isinstance(transfer_fee, dict) else {}
    fee_bps = _safe_float(current_fee.get("fee_rate")) if isinstance(current_fee, dict) else None
    transfer_fee_pct = fee_bps / 100 if fee_bps is not None else None

    score = 100
    risk_flags = []
    penalties = {
        "mintable": 20,
        "freezable": 20,
        "closable": 25,
        "metadata_mutable": 10,
        "balance_mutable": 35,
        "transfer_fee_upgradable": 10,
        "default_account_state_upgradable": 15,
        "transfer_hook_upgradable": 15,
    }
    for key, penalty in penalties.items():
        if flags[key] is True:
            score -= penalty
            risk_flags.append(key)
    if default_state == "2":
        score = 0
        risk_flags.append("default_accounts_frozen")
    if non_transferable:
        score = 0
        risk_flags.append("non_transferable")
    if hook_malicious:
        score = 0
        risk_flags.append("malicious_transfer_hook")
    if creator_malicious:
        score = 0
        risk_flags.append("malicious_creator")
    if transfer_fee_pct is not None and transfer_fee_pct > 10:
        score -= 30
        risk_flags.append("high_transfer_fee")
    elif transfer_fee_pct is not None and transfer_fee_pct > 5:
        score -= 15
        risk_flags.append("elevated_transfer_fee")

    dex_rows = token.get("dex") or []
    if isinstance(dex_rows, dict):
        dex_rows = [dex_rows]
    liquidity_values = [
        _safe_float(row.get("tvl"))
        for row in dex_rows if isinstance(row, dict)
    ]
    liquidity_values = [value for value in liquidity_values if value is not None and value >= 0]

    final_score = max(0, min(100, score))
    flags_output = {
        **{key: value is True for key, value in flags.items()},
        "default_accounts_frozen": default_state == "2",
        "non_transferable": non_transferable,
        "malicious_transfer_hook": hook_malicious,
        "malicious_creator": creator_malicious,
        "high_transfer_fee": transfer_fee_pct is not None and transfer_fee_pct > 5,
    }
    return {
        "error": None,
        "is_risky": final_score < 60 or any(flags_output.values()),
        "flags": flags_output,
        "security_score": final_score,
        "is_honeypot": None,  # Le schéma Solana documenté ne fournit pas ce champ EVM.
        "owner_renounced": None,  # Les champs Solana ne prouvent pas une renonciation globale.
        "source_verified": None,
        "is_mintable": flags["mintable"],
        "is_freezable": flags["freezable"],
        "is_closable": flags["closable"],
        "is_balance_mutable": flags["balance_mutable"],
        "non_transferable": non_transferable,
        "default_account_state": default_state,
        "malicious_transfer_hook": hook_malicious,
        "malicious_creator": creator_malicious,
        "transfer_fee_pct": transfer_fee_pct,
        "dex_liquidity_usd": max(liquidity_values) if liquidity_values else None,
        "risk_flags": risk_flags,
    }



def token_security_allows_buy(result: dict, min_score: float = 60) -> bool:
    """Fail closed unless the provider returned complete, explicitly non-risky data."""
    if not isinstance(result, dict) or result.get("error") is not None:
        return False
    if result.get("is_risky") is not False or result.get("is_honeypot") is True:
        return False
    try:
        score = float(result.get("security_score"))
        threshold = float(min_score)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(score) or not math.isfinite(threshold):
        return False
    return score >= threshold
