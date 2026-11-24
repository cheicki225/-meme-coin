"""Conservative accounting helpers for confirmed Solana transactions.

The functions in this module only interpret RPC transaction metadata. They do
not sign, submit, or retry transactions. Token amounts remain integer base
units until a caller explicitly converts them using the mint decimals.
"""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any

LAMPORTS_PER_SOL = 1_000_000_000
ACCOUNTING_SCHEMA_VERSION = 1
VALUATION_SCHEMA_VERSION = 1
DEFAULT_MAX_SOL_USD_RATE_AGE_S = 300


class AccountingError(ValueError):
    """Raised when confirmed metadata is insufficient for reliable accounting."""


def _nonnegative_int(value: Any, field: str) -> int:
    """Parse RPC integer fields without bool coercion or float truncation."""
    if type(value) is int:
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        try:
            parsed = int(value)
        except ValueError as exc:
            raise AccountingError(f"{field} is outside the supported integer range") from exc
    else:
        raise AccountingError(f"{field} must be an integer or an ASCII digit string")
    if parsed < 0:
        raise AccountingError(f"{field} cannot be negative")
    return parsed


def _pubkey(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        key = value.get("pubkey")
        return key if isinstance(key, str) else None
    return None


def _finite_number(value: Any) -> float | None:
    """Return a finite numeric value, rejecting bool and numeric strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except (OverflowError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _transaction_timestamp(transaction: dict) -> float | None:
    """Read an optional RPC block timestamp without fabricating one."""
    return _finite_number(transaction.get("blockTime"))


def value_confirmed_sol_cashflow_usd(
    accounting: dict,
    *,
    sol_usd_rate: Any = None,
    rate_timestamp: Any = None,
    rate_source: Any = None,
    rate_verified: Any = False,
    event_timestamp: Any = None,
    max_rate_age_s: Any = DEFAULT_MAX_SOL_USD_RATE_AGE_S,
) -> dict:
    """Value confirmed SOL cash flow only from a verified, time-aligned rate.

    Missing, stale, future-dated, unverified, or malformed rate evidence is
    represented explicitly as ``valuation_status='unknown'``. The returned
    SOL cash flow uses the confirmed wallet balance delta, which already
    includes the network fee and any rent reflected in those balances. The
    network fee is also valued separately for fee reporting, so callers must
    not subtract it from ``sol_cashflow_usd`` a second time. ``rate_verified``
    is a caller attestation: this function checks the supplied source and
    timestamp but cannot authenticate an external provider response.
    """
    reason = None
    if not isinstance(accounting, dict):
        reason = "missing_accounting"
    elif accounting.get("accounting_source") != "confirmed_rpc_transaction_meta":
        reason = "unconfirmed_accounting_source"
    elif type(accounting.get("accounting_schema_version")) is not int or accounting["accounting_schema_version"] != ACCOUNTING_SCHEMA_VERSION:
        reason = "unsupported_accounting_schema"
    elif type(accounting.get("native_delta_lamports")) is not int:
        reason = "invalid_native_delta"
    elif type(accounting.get("fee_lamports")) is not int or accounting["fee_lamports"] < 0:
        reason = "invalid_confirmed_fee"

    rate = _finite_number(sol_usd_rate)
    rate_time = _finite_number(rate_timestamp)
    event_time = _finite_number(
        accounting.get("event_timestamp") if event_timestamp is None and isinstance(accounting, dict)
        else event_timestamp
    )
    max_age = _finite_number(max_rate_age_s)
    if reason is None:
        if rate is None or rate <= 0:
            reason = "missing_or_invalid_rate"
        elif rate_time is None:
            reason = "missing_rate_timestamp"
        elif event_time is None:
            reason = "missing_event_timestamp"
        elif max_age is None or max_age < 0:
            reason = "invalid_max_rate_age"
        elif max_age > DEFAULT_MAX_SOL_USD_RATE_AGE_S:
            reason = "invalid_max_rate_age"
        elif rate_verified is not True:
            reason = "rate_not_verified"
        elif not isinstance(rate_source, str) or not rate_source.strip():
            reason = "missing_rate_source"
        elif rate_time > event_time:
            reason = "rate_after_accounting_event"
        elif event_time - rate_time > max_age:
            reason = "stale_rate"

    base = {
        "accounting_schema_version": (
            accounting.get("accounting_schema_version") if isinstance(accounting, dict) else None
        ),
        "valuation_schema_version": VALUATION_SCHEMA_VERSION,
        "accounting_source": (
            accounting.get("accounting_source") if isinstance(accounting, dict) else None
        ),
        "valuation_source": "timestamped_sol_usd_rate",
        "event_timestamp": event_time,
        "fee_currency": "SOL",
        "fee_status": "confirmed" if reason not in {
            "missing_accounting", "unconfirmed_accounting_source", "unsupported_accounting_schema",
            "invalid_confirmed_fee",
        } else "unknown",
        "valuation_status": "unknown",
        "valuation_reason": reason,
        "sol_cashflow_usd": None,
        "fee_usd": None,
        "sol_usd_rate": rate,
        "sol_usd_rate_timestamp": rate_time,
        "sol_usd_rate_source": rate_source.strip() if isinstance(rate_source, str) and rate_source.strip() else None,
        "sol_usd_rate_verified": rate_verified is True,
        "max_rate_age_s": max_age,
        "rate_age_seconds": None,
        "fee_included_in_sol_cashflow": True,
    }
    if reason is not None:
        return base
    # The status checks above narrow these values for both runtime and mypy.
    assert rate is not None
    assert rate_time is not None
    assert event_time is not None

    try:
        rate_decimal = Decimal(str(sol_usd_rate))
        native_delta_sol = Decimal(accounting["native_delta_lamports"]) / Decimal(LAMPORTS_PER_SOL)
        fee_sol = Decimal(accounting["fee_lamports"]) / Decimal(LAMPORTS_PER_SOL)
        cashflow_usd = float(native_delta_sol * rate_decimal)
        fee_usd = float(fee_sol * rate_decimal)
    except (InvalidOperation, OverflowError, ValueError):
        base["valuation_reason"] = "invalid_rate_conversion"
        return base
    if not math.isfinite(cashflow_usd) or not math.isfinite(fee_usd):
        base["valuation_reason"] = "invalid_rate_conversion"
        return base

    base.update({
        "valuation_status": "valued",
        "valuation_reason": None,
        "sol_cashflow_usd": cashflow_usd,
        "fee_usd": fee_usd,
        "sol_usd_rate": rate,
        "sol_usd_rate_timestamp": rate_time,
        "sol_usd_rate_source": rate_source.strip(),
        "sol_usd_rate_verified": True,
        "max_rate_age_s": max_age,
        "rate_age_seconds": event_time - rate_time,
    })
    return base


def _token_amounts(rows: Any, wallet: str) -> tuple[dict[str, int], dict[str, int]]:
    amounts: dict[str, int] = {}
    decimals: dict[str, int] = {}
    for row in rows or []:
        if not isinstance(row, dict) or row.get("owner") != wallet:
            continue
        mint = row.get("mint")
        amount = (row.get("uiTokenAmount") or {}).get("amount")
        precision = (row.get("uiTokenAmount") or {}).get("decimals")
        if not isinstance(mint, str) or not mint:
            continue
        try:
            amounts[mint] = amounts.get(mint, 0) + _nonnegative_int(amount, "token amount")
            precision = _nonnegative_int(precision, "token decimals")
        except (AccountingError, TypeError, OverflowError) as exc:
            raise AccountingError("Token balance metadata is malformed") from exc
        previous = decimals.setdefault(mint, precision)
        if previous != precision:
            raise AccountingError(f"Inconsistent decimals for mint {mint}")
    return amounts, decimals


def account_confirmed_swap(
    wallet: str,
    input_mint: str,
    output_mint: str,
    transaction: dict,
) -> dict:
    """Return realized wallet balance deltas and the network fee.

    `native_delta_lamports` is the fee-inclusive change to the wallet's SOL
    account. For SOL buys, total cash cost is its negative; for SOL sells, net
    cash proceeds are its positive. This naturally includes rent paid or
    returned in the same transaction, which is required for wallet cash-flow
    PnL. Token deltas are summed by mint across all accounts owned by `wallet`.
    """
    if not wallet or input_mint == output_mint:
        raise AccountingError("Wallet and distinct swap mints are required")
    if not isinstance(transaction, dict):
        raise AccountingError("Confirmed transaction is missing")
    meta = transaction.get("meta")
    if not isinstance(meta, dict):
        raise AccountingError("Confirmed transaction metadata is missing")
    if meta.get("err") is not None:
        raise AccountingError("Transaction failed on chain")

    message = ((transaction.get("transaction") or {}).get("message") or {})
    keys = [_pubkey(key) for key in message.get("accountKeys", [])]
    if wallet not in keys:
        raise AccountingError("Wallet account is missing from transaction metadata")
    wallet_index = keys.index(wallet)
    pre_balances = meta.get("preBalances") or []
    post_balances = meta.get("postBalances") or []
    if wallet_index >= len(pre_balances) or wallet_index >= len(post_balances):
        raise AccountingError("Wallet SOL balances are incomplete")
    try:
        native_delta = (
            _nonnegative_int(post_balances[wallet_index], "post SOL balance")
            - _nonnegative_int(pre_balances[wallet_index], "pre SOL balance")
        )
        fee = _nonnegative_int(meta["fee"], "transaction fee")
    except (KeyError, TypeError, AccountingError, OverflowError) as exc:
        raise AccountingError("SOL balance or fee metadata is malformed") from exc
    if not keys or keys[0] != wallet:
        raise AccountingError("Wallet is not the transaction fee payer")

    before, decimals_before = _token_amounts(meta.get("preTokenBalances"), wallet)
    after, decimals_after = _token_amounts(meta.get("postTokenBalances"), wallet)
    mint_decimals = dict(decimals_before)
    for mint, precision in decimals_after.items():
        if mint in mint_decimals and mint_decimals[mint] != precision:
            raise AccountingError(f"Inconsistent decimals for mint {mint}")
        mint_decimals[mint] = precision
    deltas = {mint: after.get(mint, 0) - before.get(mint, 0) for mint in set(before) | set(after)}
    deltas = {mint: delta for mint, delta in deltas.items() if delta}
    if input_mint != "So11111111111111111111111111111111111111112" and deltas.get(input_mint, 0) >= 0:
        raise AccountingError("Input token was not spent according to confirmed balances")
    if output_mint != "So11111111111111111111111111111111111111112" and deltas.get(output_mint, 0) <= 0:
        raise AccountingError("Output token was not received according to confirmed balances")
    if input_mint == "So11111111111111111111111111111111111111112" and native_delta >= 0:
        raise AccountingError("SOL input did not decrease the wallet balance")
    if output_mint == "So11111111111111111111111111111111111111112" and native_delta <= 0:
        raise AccountingError("SOL output did not increase the wallet balance")

    # Any unrelated token movement makes this transaction unsafe to treat as a
    # simple swap. The Jupiter route may use temporary WSOL accounts, but those
    # are not owned by the wallet and therefore do not appear in these deltas.
    allowed_mints = {input_mint, output_mint}
    unexpected = {mint: delta for mint, delta in deltas.items() if mint not in allowed_mints}
    if unexpected:
        raise AccountingError("Transaction changed unrelated wallet token balances")

    return {
        "accounting_schema_version": ACCOUNTING_SCHEMA_VERSION,
        "wallet": wallet,
        "input_mint": input_mint,
        "output_mint": output_mint,
        "native_delta_lamports": native_delta,
        "fee_lamports": fee,
        "token_deltas_raw": deltas,
        "token_decimals": mint_decimals,
        "input_delta_raw": deltas.get(input_mint),
        "output_delta_raw": deltas.get(output_mint),
        "accounting_source": "confirmed_rpc_transaction_meta",
        "event_timestamp": _transaction_timestamp(transaction),
    }


def account_confirmed_sol_transfer(
    wallet: str,
    destination: str,
    amount_lamports: int,
    transaction: dict,
) -> dict:
    """Verify a simple native SOL transfer from confirmed balance deltas."""
    if not wallet or not destination or wallet == destination:
        raise AccountingError("Distinct source and destination wallets are required")
    if type(amount_lamports) is not int or amount_lamports <= 0:
        raise AccountingError("Transfer amount must be a positive integer")
    if not isinstance(transaction, dict) or not isinstance(transaction.get("meta"), dict):
        raise AccountingError("Confirmed transfer metadata is missing")
    meta = transaction["meta"]
    if meta.get("err") is not None:
        raise AccountingError("Transfer failed on chain")
    message = ((transaction.get("transaction") or {}).get("message") or {})
    keys = [_pubkey(key) for key in message.get("accountKeys", [])]
    if not keys or keys[0] != wallet or destination not in keys:
        raise AccountingError("Unexpected transfer payer or destination")
    source_index, destination_index = keys.index(wallet), keys.index(destination)
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if max(source_index, destination_index) >= min(len(pre), len(post)):
        raise AccountingError("Transfer balance metadata is incomplete")
    try:
        fee = _nonnegative_int(meta["fee"], "transaction fee")
        source_delta = (
            _nonnegative_int(post[source_index], "post SOL balance")
            - _nonnegative_int(pre[source_index], "pre SOL balance")
        )
        destination_delta = (
            _nonnegative_int(post[destination_index], "post SOL balance")
            - _nonnegative_int(pre[destination_index], "pre SOL balance")
        )
    except (KeyError, TypeError, AccountingError, OverflowError) as exc:
        raise AccountingError("Transfer balance or fee metadata is malformed") from exc
    if source_delta != -(amount_lamports + fee) or destination_delta != amount_lamports:
        raise AccountingError("Confirmed SOL deltas do not match the requested transfer")
    if meta.get("preTokenBalances") or meta.get("postTokenBalances"):
        before, _ = _token_amounts(meta.get("preTokenBalances"), wallet)
        after, _ = _token_amounts(meta.get("postTokenBalances"), wallet)
        if before != after:
            raise AccountingError("Transfer also changed wallet token balances")
    return {
        "accounting_schema_version": ACCOUNTING_SCHEMA_VERSION,
        "wallet": wallet,
        "destination": destination,
        "amount_lamports": amount_lamports,
        "native_delta_lamports": source_delta,
        "destination_delta_lamports": destination_delta,
        "fee_lamports": fee,
        "accounting_source": "confirmed_rpc_transaction_meta",
        "event_timestamp": _transaction_timestamp(transaction),
    }

