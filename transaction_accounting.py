"""Conservative accounting helpers for confirmed Solana transactions.

The functions in this module only interpret RPC transaction metadata. They do
not sign, submit, or retry transactions. Token amounts remain integer base
units until a caller explicitly converts them using the mint decimals.
"""
from __future__ import annotations

from typing import Any


class AccountingError(ValueError):
    """Raised when confirmed metadata is insufficient for reliable accounting."""


def _pubkey(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        key = value.get("pubkey")
        return key if isinstance(key, str) else None
    return None


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
            amounts[mint] = amounts.get(mint, 0) + int(amount)
            precision = int(precision)
        except (TypeError, ValueError, OverflowError) as exc:
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
        native_delta = int(post_balances[wallet_index]) - int(pre_balances[wallet_index])
        fee = int(meta["fee"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise AccountingError("SOL balance or fee metadata is malformed") from exc
    if fee < 0:
        raise AccountingError("Transaction fee cannot be negative")
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
    if not isinstance(amount_lamports, int) or isinstance(amount_lamports, bool) or amount_lamports <= 0:
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
        fee = int(meta["fee"])
        source_delta = int(post[source_index]) - int(pre[source_index])
        destination_delta = int(post[destination_index]) - int(pre[destination_index])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise AccountingError("Transfer balance or fee metadata is malformed") from exc
    if fee < 0 or source_delta != -(amount_lamports + fee) or destination_delta != amount_lamports:
        raise AccountingError("Confirmed SOL deltas do not match the requested transfer")
    if meta.get("preTokenBalances") or meta.get("postTokenBalances"):
        before, _ = _token_amounts(meta.get("preTokenBalances"), wallet)
        after, _ = _token_amounts(meta.get("postTokenBalances"), wallet)
        if before != after:
            raise AccountingError("Transfer also changed wallet token balances")
    return {
        "wallet": wallet,
        "destination": destination,
        "amount_lamports": amount_lamports,
        "native_delta_lamports": source_delta,
        "destination_delta_lamports": destination_delta,
        "fee_lamports": fee,
        "accounting_source": "confirmed_rpc_transaction_meta",
    }

