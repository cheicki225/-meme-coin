"""
════════════════════════════════════════════════════════════════
SÉCURITÉ TOKEN — Vérification via GoPlus Security API Solana
════════════════════════════════════════════════════════════════
Évalue les autorités SPL et les risques Token-2022 exposés par le schéma
GoPlus Solana. Ce score indicatif n'est pas un audit de programme ni une
garantie de liquidité ou de possibilité de vente.

La bonding curve Pump.fun ne garantit pas qu'un token soit sûr. GoPlus
peut aussi ne pas avoir de données DEX pour les tokens très récents; ces
réponses sont traitées comme inconnues et bloquées lorsque le filtre est activé.

Activé par défaut pour les nouveaux wallets. Une réponse absente, invalide ou
incomplète reste UNKNOWN et bloque l'achat; un réglage explicitement conservé
sur un wallet existant n'est pas réécrit automatiquement.
"""

import logging
import math
import aiohttp

import config
import rpc_client

log = logging.getLogger("security")


async def check_token_security(token_mint: str) -> dict:
    """
    Retourne un score indicatif basé sur les contrôles natifs Solana GoPlus.
    Les champs EVM `buy_tax`, `sell_tax`, `is_honeypot` et `is_open_source`
    ne sont pas utilisés comme s'ils existaient dans le schéma Solana.
    """
    # GoPlus' public Solana endpoint accepts unauthenticated GET requests.
    # Add auth headers only when the user configured credentials.
    url = f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={token_mint}"
    headers = {"Accept": "application/json"}
    if config.GOPLUS_API_KEY:
        headers["x-api-key"] = config.GOPLUS_API_KEY
    if config.GOPLUS_API_SECRET:
        headers["Authorization"] = f"Bearer {config.GOPLUS_API_SECRET}"

    try:
        async with aiohttp.ClientSession(connector=rpc_client.get_http_connector(), connector_owner=False) as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    log.debug(f"GoPlus statut {resp.status} pour {token_mint[:8]}...")
                    return _unavailable(f"http_{resp.status}")
                data = await resp.json()
    except Exception as e:
        log.debug(f"Erreur GoPlus pour {token_mint[:8]}...: {type(e).__name__}")
        return _unavailable("provider_unavailable")

    return _parse_token_security(data, token_mint)


def _parse_token_security(data: dict, token_mint: str) -> dict:
    """Parse GoPlus' Solana mint-authority and Token-2022 response schema.

    Missing or malformed control data is unknown and blocks the caller. A
    successful result is still only an automated indicator: GoPlus does not
    return the EVM honeypot/tax fields used by the older parser.
    """
    if not isinstance(data, dict):
        return _unavailable("invalid_response")

    code = data.get("code")
    if not ((type(code) is int and code == 1) or code == "1"):
        return _unavailable("provider_error")

    result = data.get("result")
    if not isinstance(result, dict):
        return _unavailable("invalid_result")
    token_data = result.get(token_mint.lower(), result.get(token_mint, {}))

    if not token_data:
        # Très courant pour un token Pump.fun tout juste créé — pas encore indexé.
        return _unavailable("not_indexed")
    if not isinstance(token_data, dict):
        return _unavailable("invalid_token_data")

    try:
        mintable, mint_authority_malicious = _control_state(token_data, "mintable")
        freezable, freeze_authority_malicious = _control_state(token_data, "freezable")
        closable, close_authority_malicious = _control_state(token_data, "closable")
        metadata_mutable, metadata_authority_malicious = _control_state(token_data, "metadata_mutable")
        account_state = _account_state(token_data)
        non_transferable = _parse_binary_flag(
            token_data.get("non_transferable"), "non_transferable"
        )
        transfer_fee_bps = _transfer_fee_bps(token_data)
        transfer_hook, transfer_hook_malicious = _transfer_hook_state(token_data)
        max_dex_tvl = _max_dex_tvl(token_data)
        lp_locked_pct = _lp_locked_percentage(token_data)
    except ValueError as exc:
        return _unavailable(str(exc))

    score = 100
    hard_risk = (
        account_state != 1
        or non_transferable
        or mint_authority_malicious
        or freeze_authority_malicious
        or close_authority_malicious
        or metadata_authority_malicious
        or transfer_hook_malicious
        or transfer_fee_bps >= 10_000
        # Empty LP-holder results occur for some concentrated pools, but they
        # do not establish whether liquidity can be withdrawn. Keep the
        # enabled safety filter fail-closed while that risk input is unknown.
        or lp_locked_pct is None
    )
    if hard_risk:
        score = 0
        lp_lock_penalty = 0
    else:
        # This percentage is computed only from the LP-holder rows GoPlus
        # returned (typically the top holders, not the full pool). Penalize
        # weak reported locking without treating the partial list as a full
        # liquidity audit: <25% => -45, <50% => -30, <80% => -15, else 0.
        lp_lock_penalty = _lp_lock_penalty(lp_locked_pct)
        score -= lp_lock_penalty
        if mintable:
            score -= 50
        if freezable:
            score -= 50
        if closable:
            score -= 35
        if metadata_mutable:
            score -= 20
        if transfer_hook:
            score -= 45
        if transfer_fee_bps >= 2_000:
            score -= 60
        elif transfer_fee_bps >= 500:
            score -= 45
        elif transfer_fee_bps >= 100:
            score -= 30
        elif transfer_fee_bps > 0:
            score -= 10
    score = max(0, min(100, score))

    return {
        "risk_model": "goplus_solana_authorities_v1",
        # GoPlus' Solana schema does not provide the classic EVM honeypot or
        # buy/sell-tax fields. None means "not assessed", not "safe".
        "is_honeypot": None,
        "honeypot_detection_available": False,
        "buy_tax": None,
        "sell_tax": None,
        "lp_locked_pct": lp_locked_pct,
        "lp_lock_data_available": lp_locked_pct is not None,
        "lp_lock_penalty": lp_lock_penalty,
        "owner_renounced": None,
        "is_mintable": mintable,
        "is_freezable": freezable,
        "is_closable": closable,
        "metadata_mutable": metadata_mutable,
        "non_transferable": non_transferable,
        "default_account_state": account_state,
        "transfer_fee_bps": transfer_fee_bps,
        "transfer_hook_present": transfer_hook,
        "max_dex_tvl_usd": max_dex_tvl,
        "security_score": score,
        "error": None,
    }


def _unavailable(error: str) -> dict:
    return {
        "error": error,
        "security_score": 0,
        "is_honeypot": None,
        "honeypot_detection_available": False,
    }


def _parse_binary_flag(value, field: str) -> bool:
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return value == 1
    if isinstance(value, str) and value in ("0", "1"):
        return value == "1"
    raise ValueError(f"missing_or_invalid_{field}")


def _control_state(token_data: dict, field: str) -> tuple[bool, bool]:
    """Return active state and whether an associated authority is flagged."""
    control = token_data.get(field)
    if not isinstance(control, dict):
        raise ValueError(f"missing_or_invalid_{field}")
    active = _parse_binary_flag(control.get("status"), f"{field}.status")
    authority_field = "metadata_upgrade_authority" if field == "metadata_mutable" else "authority"
    authorities = control.get(authority_field)
    if not isinstance(authorities, list):
        raise ValueError(f"missing_or_invalid_{field}.{authority_field}")
    if not active and authorities:
        raise ValueError(f"inconsistent_{field}_authority")
    malicious = False
    for authority in authorities:
        if (
            not isinstance(authority, dict)
            or not isinstance(authority.get("address"), str)
            or not authority["address"].strip()
        ):
            raise ValueError(f"invalid_{field}.authority")
        malicious = malicious or _parse_binary_flag(
            authority.get("malicious_address"), f"{field}.malicious_address"
        )
    return active, malicious


def _account_state(token_data: dict) -> int:
    value = token_data.get("default_account_state")
    if (type(value) is int and value == 1) or value == "1":
        return 1
    if (type(value) is int and value == 0) or value == "0":
        return 0
    if (type(value) is int and value == 2) or value == "2":
        return 2
    raise ValueError("missing_or_invalid_default_account_state")


def _integer_rate(value, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"invalid_{field}")
    if isinstance(value, int):
        rate = value
    elif isinstance(value, str) and value.isdecimal():
        rate = int(value)
    else:
        raise ValueError(f"invalid_{field}")
    if not 0 <= rate <= 10_000:
        raise ValueError(f"invalid_{field}")
    return rate


def _transfer_fee_bps(token_data: dict) -> int:
    transfer_fee = token_data.get("transfer_fee")
    if not isinstance(transfer_fee, dict):
        raise ValueError("missing_or_invalid_transfer_fee")
    rates = []
    for field in ("current_fee_rate", "scheduled_fee_rate"):
        if field not in transfer_fee:
            continue
        rate_data = transfer_fee[field]
        if not isinstance(rate_data, dict) or "fee_rate" not in rate_data:
            raise ValueError(f"invalid_transfer_fee.{field}")
        rates.append(_integer_rate(rate_data["fee_rate"], f"transfer_fee.{field}"))
    if transfer_fee and not rates:
        raise ValueError("unknown_transfer_fee")
    return max(rates, default=0)


def _transfer_hook_state(token_data: dict) -> tuple[bool, bool]:
    hooks = token_data.get("transfer_hook")
    if not isinstance(hooks, list):
        raise ValueError("missing_or_invalid_transfer_hook")
    malicious = False
    for hook in hooks:
        if not isinstance(hook, dict) or not isinstance(hook.get("address"), str):
            raise ValueError("invalid_transfer_hook")
        malicious = malicious or _parse_binary_flag(
            hook.get("malicious_address"), "transfer_hook.malicious_address"
        )
    return bool(hooks), malicious


def _max_dex_tvl(token_data: dict) -> float:
    dexes = token_data.get("dex")
    if not isinstance(dexes, list):
        raise ValueError("missing_or_invalid_dex")
    if not dexes:
        raise ValueError("no_dex_liquidity")
    tvls = []
    for dex in dexes:
        if not isinstance(dex, dict):
            raise ValueError("invalid_dex")
        tvl = _finite_number(dex.get("tvl"), "dex.tvl")
        if tvl < 0:
            raise ValueError("invalid_dex.tvl")
        tvls.append(tvl)
    if max(tvls, default=0) <= 0:
        raise ValueError("no_dex_liquidity")
    return max(tvls)


def _lp_locked_percentage(token_data: dict) -> float | None:
    holders = token_data.get("lp_holders")
    if not isinstance(holders, list):
        raise ValueError("missing_or_invalid_lp_holders")
    if not holders:
        # Concentrated liquidity pools may not have fungible LP tokens. The
        # endpoint can still provide valid DEX TVL, but lock percentage is
        # unavailable and must remain explicitly unknown.
        return None
    locked_percent = 0.0
    total_percent = 0.0
    for holder in holders:
        if not isinstance(holder, dict):
            raise ValueError("invalid_lp_holder")
        locked = _parse_binary_flag(holder.get("is_locked"), "lp_holder.is_locked")
        percent = _finite_number(holder.get("percent"), "lp_holder.percent")
        if not 0 <= percent <= 1:
            raise ValueError("invalid_lp_holder.percent")
        total_percent += percent
        if locked:
            locked_percent += percent * 100
    if total_percent > 1.000001:
        raise ValueError("inconsistent_lp_holder_percentage_total")
    return locked_percent


def _lp_lock_penalty(lp_locked_pct: float) -> int:
    """Apply a conservative penalty to the non-exhaustive GoPlus LP sample."""
    if lp_locked_pct < 25:
        return 45
    if lp_locked_pct < 50:
        return 30
    if lp_locked_pct < 80:
        return 15
    return 0


def _finite_number(value, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"invalid_{field}")
    try:
        value = float(value)
        if math.isfinite(value):
            return value
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"invalid_{field}") from None
    raise ValueError(f"invalid_{field}")


def is_valid_solana_security_result(result) -> bool:
    """Check the complete parsed GoPlus contract before a caller trusts its score."""
    if not isinstance(result, dict) or result.get("error") is not None:
        return False
    if result.get("risk_model") != "goplus_solana_authorities_v1":
        return False
    if type(result.get("security_score")) is not int or not 0 <= result["security_score"] <= 100:
        return False
    if result.get("lp_lock_data_available") is not True:
        return False
    lp_locked = result.get("lp_locked_pct")
    if not isinstance(lp_locked, (int, float)) or isinstance(lp_locked, bool):
        return False
    dex_tvl = result.get("max_dex_tvl_usd")
    if not isinstance(dex_tvl, (int, float)) or isinstance(dex_tvl, bool):
        return False
    try:
        lp_locked = float(lp_locked)
        dex_tvl = float(dex_tvl)
    except (OverflowError, TypeError, ValueError):
        return False
    if not math.isfinite(lp_locked) or not 0 <= lp_locked <= 100:
        return False
    if not math.isfinite(dex_tvl) or dex_tvl <= 0:
        return False
    boolean_fields = (
        "is_mintable", "is_freezable", "is_closable", "metadata_mutable",
        "non_transferable", "transfer_hook_present",
    )
    if any(type(result.get(field)) is not bool for field in boolean_fields):
        return False
    if type(result.get("default_account_state")) is not int or result["default_account_state"] not in (0, 1, 2):
        return False
    fee_bps = result.get("transfer_fee_bps")
    if type(fee_bps) is not int or not 0 <= fee_bps <= 10_000:
        return False
    return (
        result.get("is_honeypot") is None
        and result.get("honeypot_detection_available") is False
        and result.get("buy_tax") is None
        and result.get("sell_tax") is None
    )


def format_security_report(token_mint: str, result) -> str:
    """Format a user-facing Solana risk summary without implying a guarantee."""
    header = f"🛡️ Analyse de sécurité GoPlus\nToken: {token_mint}"
    if not is_valid_solana_security_result(result):
        return (
            f"{header}\n\n⚪ État inconnu / indisponible. "
            "La vérification n'a pas fourni des données Solana complètes; "
            "aucune conclusion de sécurité n'est possible. "
            "Le filtre refuse l'achat si ces données sont inconnues."
        )

    score = result["security_score"]
    icon = "🔴" if score < 60 else "⚠️"
    lines = [
        header,
        "",
        f"{icon} Indicateur GoPlus: {score}/100",
        "Les données GoPlus ne garantissent ni la sécurité ni la possibilité de vente.",
        "",
        "Contrôles Solana retournés:",
    ]
    risks = []
    if result.get("is_mintable"):
        risks.append("Autorité de mint active")
    if result.get("is_freezable"):
        risks.append("Autorité de gel active")
    if result.get("is_closable"):
        risks.append("Fermeture du token possible")
    if result.get("metadata_mutable"):
        risks.append("Métadonnées modifiables")
    if result.get("non_transferable"):
        risks.append("Token non transférable")
    if result.get("default_account_state") == 2:
        risks.append("État des nouveaux comptes gelé")
    if result.get("transfer_hook_present"):
        risks.append("Transfer hook présent")
    if result.get("transfer_fee_bps", 0) > 0:
        risks.append(f"Frais de transfert: {result['transfer_fee_bps']} points de base")
    if risks:
        lines.extend(f"⚠️ {risk}" for risk in risks)
    else:
        lines.append("Aucune des autorités à risque listées n'est active dans les champs reçus.")

    tvl = result.get("max_dex_tvl_usd")
    if isinstance(tvl, (int, float)) and not isinstance(tvl, bool):
        lines.append(f"TVL DEX maximale rapportée: {tvl:,.2f} USD")
    if result.get("lp_locked_pct") is None:
        lines.append("Verrouillage LP: inconnu / non disponible; score bloquant")
    else:
        lines.append(
            "Part LP verrouillée rapportée par les lignes GoPlus: "
            f"{result['lp_locked_pct']:.1f}% (couverture API non exhaustive)"
        )
        if result.get("lp_lock_penalty", 0) > 0:
            lines.append(f"Pénalité appliquée au score pour le verrouillage LP: -{result['lp_lock_penalty']} points")
    lines.append("Taxe buy/sell et détection honeypot EVM: non fournies par ce schéma Solana.")
    return "\n".join(lines)
