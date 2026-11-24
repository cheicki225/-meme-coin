"""
Wallet Data Collector (read-only).

Collecte les transactions Pump.fun observées et conserve des événements bruts
normalisés dans SQLite. Les mouvements de soldes sont des candidats d'achat/
vente, pas une preuve suffisante d'un swap ni un calcul de PnL. Les prix et
PnL restent explicitement inconnus tant qu'un parseur de swap fiable n'existe.
Aucune signature, aucun ordre et aucune fonction d'exécution ne sont appelés.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import config
import wallet_history

log = logging.getLogger("wallet_data_collector")
DEFAULT_DB_PATH = Path(getattr(config, "WALLET_ACTIVITY_DB_FILE", "data/wallet_intelligence.sqlite3"))
COLLECTION_SOURCE = "solana_rpc_getTransaction_pump_fun_balance_deltas_v1"
MAX_TOKEN_AMOUNT = (1 << 64) - 1
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BASE58_VALUES = {character: index for index, character in enumerate(BASE58_ALPHABET)}


class WalletNormalizationError(ValueError):
    """Raised when relevant transaction data is insufficient to classify safely."""


def _account_key(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        key = value.get("pubkey")
        return key if isinstance(key, str) else None
    return None


def _is_solana_pubkey(value: Any) -> bool:
    """Return whether a string is a base58 encoding of exactly 32 bytes."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 44
        or any(char not in BASE58_VALUES for char in value)
    ):
        return False
    leading_zero_bytes = len(value) - len(value.lstrip("1"))
    decoded_value = 0
    for character in value:
        decoded_value = decoded_value * 58 + BASE58_VALUES[character]
    decoded_bytes = (decoded_value.bit_length() + 7) // 8
    return leading_zero_bytes + decoded_bytes == 32


def _wallet_token_deltas(tx: dict, wallet: str) -> list[dict]:
    meta = tx.get("meta") or {}
    pre = meta.get("preTokenBalances") or []
    post = meta.get("postTokenBalances") or []
    before, after, decimals = {}, {}, {}
    for rows, target in ((pre, before), (post, after)):
        for row in rows:
            if not isinstance(row, dict) or row.get("owner") != wallet:
                continue
            mint = row.get("mint")
            amount = (row.get("uiTokenAmount") or {}).get("amount")
            dec = (row.get("uiTokenAmount") or {}).get("decimals")
            try:
                amount_value, decimal_value = int(amount), int(dec)
            except (TypeError, ValueError):
                continue
            if mint in decimals and decimals[mint] != decimal_value:
                continue
            target[mint] = target.get(mint, 0) + amount_value
            decimals[mint] = decimal_value
    result = []
    for mint in set(before) | set(after):
        delta = after.get(mint, 0) - before.get(mint, 0)
        if delta:
            result.append({
                "mint": mint,
                "raw_delta": delta,
                "decimals": decimals.get(mint),
            })
    return result


def _require_complete_pump_token_balances(wallet: str, meta: dict) -> None:
    """Reject incomplete Pump.fun token balance metadata instead of skipping it."""
    for field in ("preTokenBalances", "postTokenBalances"):
        rows = meta.get(field)
        if not isinstance(rows, list):
            raise WalletNormalizationError("Pump.fun token balance metadata is unavailable")
        for row in rows:
            if not isinstance(row, dict):
                raise WalletNormalizationError("Pump.fun token balance row is malformed")
            owner = row.get("owner")
            if not isinstance(owner, str) or not owner.strip():
                raise WalletNormalizationError("Pump.fun token balance owner is unavailable")
            if owner != wallet:
                continue
            mint = row.get("mint")
            token_amount = row.get("uiTokenAmount")
            amount = token_amount.get("amount") if isinstance(token_amount, dict) else None
            decimals = token_amount.get("decimals") if isinstance(token_amount, dict) else None
            if (
                not isinstance(mint, str)
                or not mint.strip()
                or not isinstance(amount, str)
                or not amount.isascii()
                or not amount.isdigit()
                or len(amount) > 20
                or int(amount) > MAX_TOKEN_AMOUNT
                or isinstance(decimals, bool)
                or not isinstance(decimals, int)
                or not 0 <= decimals <= 255
            ):
                raise WalletNormalizationError("Wallet token balance row is incomplete")


def _require_complete_instruction_metadata(tx: dict, meta: dict) -> None:
    """Reject RPC instruction shapes that could hide a Pump.fun invocation."""
    transaction = tx.get("transaction")
    message = transaction.get("message") if isinstance(transaction, dict) else None
    instructions = message.get("instructions") if isinstance(message, dict) else None
    if not isinstance(instructions, list):
        raise WalletNormalizationError("Top-level transaction instructions are unavailable")

    inner_groups = meta.get("innerInstructions")
    if not isinstance(inner_groups, list):
        raise WalletNormalizationError("Inner transaction instructions are unavailable")

    for instruction in instructions:
        if (
            not isinstance(instruction, dict)
            or not _is_solana_pubkey(instruction.get("programId"))
        ):
            raise WalletNormalizationError("Top-level transaction instruction is malformed")

    for group in inner_groups:
        inner_instructions = group.get("instructions") if isinstance(group, dict) else None
        if not isinstance(inner_instructions, list):
            raise WalletNormalizationError("Inner transaction instruction group is malformed")
        for instruction in inner_instructions:
            if (
                not isinstance(instruction, dict)
                or not _is_solana_pubkey(instruction.get("programId"))
            ):
                raise WalletNormalizationError("Inner transaction instruction is malformed")


def normalize_transaction(wallet: str, signature: str, tx: dict) -> dict | None:
    """Normalise un mouvement observé; ne prétend pas identifier un swap certain."""
    if not isinstance(tx, dict) or not isinstance(tx.get("meta"), dict):
        return None
    meta = tx["meta"]
    if meta.get("err") is not None:
        return None
    _require_complete_instruction_metadata(tx, meta)
    pump_fun = wallet_history._transaction_involves_program(tx, config.PUMP_FUN_PROGRAM_ID)
    if not pump_fun:
        return None
    _require_complete_pump_token_balances(wallet, meta)
    message = (tx.get("transaction") or {}).get("message") or {}
    keys = [_account_key(k) for k in message.get("accountKeys", [])]
    try:
        idx = keys.index(wallet)
        pre, post = meta.get("preBalances", []), meta.get("postBalances", [])
        if idx >= len(pre) or idx >= len(post):
            sol_delta = None
        else:
            # Exclut les frais réseau du delta SOL, mais pas les autres mouvements.
            fee = int(meta.get("fee") or 0) if keys and keys[0] == wallet else 0
            sol_delta = (int(post[idx]) - int(pre[idx]) + fee) / 1_000_000_000
    except (ValueError, TypeError, IndexError):
        sol_delta = None

    token_deltas = _wallet_token_deltas(tx, wallet)
    if not token_deltas:
        return None

    # Une transaction peut affecter plusieurs mints; stocker un événement par mint.
    events = []
    for item in token_deltas:
        direction = "buy_candidate" if item["raw_delta"] > 0 else "sell_candidate"
        # Pour limiter les faux positifs, exige un delta SOL opposé au mouvement token.
        sol_consistent = (
            sol_delta is not None
            and ((direction == "buy_candidate" and sol_delta < 0)
                 or (direction == "sell_candidate" and sol_delta > 0))
        )
        events.append({
            "wallet_address": wallet,
            "signature": signature,
            "block_time": tx.get("blockTime"),
            "token_mint": item["mint"],
            "token_delta_raw": item["raw_delta"],
            "token_decimals": item["decimals"],
            "sol_delta": sol_delta,
            "event_type": direction if sol_consistent else "unclassified_movement",
            "classification_confidence": "candidate" if sol_consistent else "low",
            "realized_pnl_sol": None,
            "entry_price_sol": None,
            "exit_price_sol": None,
            "source": COLLECTION_SOURCE,
        })
    return {"signature": signature, "events": events}


def _connect(db_path: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(db_path, timeout=10)
    db.execute("PRAGMA busy_timeout = 10000")
    db.execute("PRAGMA synchronous = FULL")
    return db


def initialize_store(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = _connect(path)
    try:
        mode = db.execute("PRAGMA journal_mode = WAL").fetchone()
        if not mode or str(mode[0]).lower() != "wal":
            raise sqlite3.DatabaseError("SQLite WAL mode could not be enabled")
        integrity = db.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise sqlite3.DatabaseError(f"SQLite integrity check failed: {integrity}")
        with db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS wallet_activity (
                    signature TEXT NOT NULL,
                    wallet_address TEXT NOT NULL,
                    token_mint TEXT NOT NULL,
                    block_time INTEGER,
                    event_type TEXT NOT NULL,
                    token_delta_raw TEXT NOT NULL,
                    token_decimals INTEGER,
                    sol_delta REAL,
                    classification_confidence TEXT NOT NULL,
                    realized_pnl_sol REAL,
                    entry_price_sol REAL,
                    exit_price_sol REAL,
                    source TEXT NOT NULL,
                    collected_at INTEGER NOT NULL,
                    PRIMARY KEY (wallet_address, signature, token_mint)
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_wallet_activity_time "
                "ON wallet_activity(wallet_address, block_time)"
            )
            db.execute("""
                CREATE TABLE IF NOT EXISTS wallet_cursors (
                    wallet_address TEXT PRIMARY KEY,
                    last_signature TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
            """)
    finally:
        db.close()


def get_collection_cursor(wallet: str, db_path: str | Path = DEFAULT_DB_PATH) -> str | None:
    if not Path(db_path).exists():
        return None
    db = _connect(db_path)
    try:
        row = db.execute(
            "SELECT last_signature FROM wallet_cursors WHERE wallet_address=?", (wallet,)
        ).fetchone()
        return row[0] if row else None
    finally:
        db.close()


def set_collection_cursor(wallet: str, signature: str, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    initialize_store(db_path)
    db = _connect(db_path)
    try:
        with db:
            db.execute("""
                INSERT INTO wallet_cursors(wallet_address,last_signature,updated_at)
                VALUES(?,?,?)
                ON CONFLICT(wallet_address) DO UPDATE SET
                    last_signature=excluded.last_signature, updated_at=excluded.updated_at
            """, (wallet, signature, int(time.time())))
    finally:
        db.close()


def store_events(events: list[dict], db_path: str | Path = DEFAULT_DB_PATH) -> int:
    initialize_store(db_path)
    stored_at = int(time.time())
    rows = [
        (
            e["signature"], e["wallet_address"], e["token_mint"], e.get("block_time"),
            e["event_type"], str(e["token_delta_raw"]), e.get("token_decimals"),
            e.get("sol_delta"), e["classification_confidence"], None, None, None,
            e["source"], e.get("collected_at", stored_at),
        )
        for e in events
    ]
    db = _connect(db_path)
    try:
        with db:
            before = db.total_changes
            db.executemany("""
            INSERT OR IGNORE INTO wallet_activity
            (signature,wallet_address,token_mint,block_time,event_type,token_delta_raw,
             token_decimals,sol_delta,classification_confidence,realized_pnl_sol,
             entry_price_sol,exit_price_sol,source,collected_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, rows)
            return db.total_changes - before
    finally:
        db.close()


def read_wallet_activity(
    wallet: str,
    db_path: str | Path = DEFAULT_DB_PATH,
    limit: int = 100,
) -> list[dict]:
    if not isinstance(wallet, str) or not wallet.strip():
        raise ValueError("wallet address is required")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    if not Path(db_path).exists():
        return []
    db = _connect(db_path)
    try:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM wallet_activity WHERE wallet_address=? "
            "ORDER BY block_time DESC, collected_at DESC LIMIT ?",
            (wallet, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        db.close()


async def collect_wallet(wallet: str, limit: int = 100, db_path: str | Path = DEFAULT_DB_PATH) -> dict:
    """Collecte read-only; renvoie un état explicite et ne transforme pas les erreurs en zéro."""
    if not isinstance(wallet, str) or not wallet.strip():
        raise ValueError("wallet address is required")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    cursor = get_collection_cursor(wallet, db_path)
    try:
        signatures = await wallet_history._get_signatures(
            wallet, limit=limit, until=cursor, max_retries=1, strict=True
        )
    except Exception as exc:
        log.warning("Wallet signature collection failed (%s)", type(exc).__name__)
        return {
            "status": "error", "reason": "signature_fetch_failed", "wallet": wallet,
            "source": COLLECTION_SOURCE, "collection_scope": "bounded_signature_page",
            "signature_limit": limit, "completeness": "unknown", "cursor_advanced": False,
        }
    if not isinstance(signatures, list):
        return {
            "status": "error", "reason": "invalid_signature_response", "wallet": wallet,
            "source": COLLECTION_SOURCE, "collection_scope": "bounded_signature_page",
            "signature_limit": limit, "completeness": "unknown", "cursor_advanced": False,
        }
    if len(signatures) > limit:
        return {
            "status": "error", "reason": "signature_page_exceeds_limit", "wallet": wallet,
            "source": COLLECTION_SOURCE, "collection_scope": "bounded_signature_page",
            "signature_limit": limit, "completeness": "unknown", "cursor_advanced": False,
        }

    events, failed, invalid_signature_records = [], 0, 0
    for item in signatures:
        signature = item.get("signature") if isinstance(item, dict) else None
        if not isinstance(signature, str) or not signature.strip():
            invalid_signature_records += 1
            continue
        try:
            tx = await wallet_history._get_raw_transaction(signature)
        except Exception:
            failed += 1
            continue
        if not tx:
            failed += 1
            continue
        if not isinstance(tx, dict) or not isinstance(tx.get("meta"), dict):
            failed += 1
            continue
        try:
            normalized = normalize_transaction(wallet, signature, tx)
        except WalletNormalizationError:
            failed += 1
            continue
        if normalized:
            events.extend(normalized["events"])

    collected_at = int(time.time())
    for event in events:
        event["collected_at"] = collected_at

    cursor_advanced = False
    newest_item = signatures[0] if signatures else None
    newest_signature = newest_item.get("signature") if isinstance(newest_item, dict) else None
    try:
        inserted = store_events(events, db_path)
        if failed == 0 and invalid_signature_records == 0 and newest_signature:
            set_collection_cursor(wallet, newest_signature, db_path)
            cursor_advanced = True
    except (OSError, sqlite3.Error) as exc:
        log.error("Wallet activity persistence failed (%s)", type(exc).__name__)
        return {
            "status": "error", "reason": "storage_failed", "wallet": wallet,
            "source": COLLECTION_SOURCE, "collection_scope": "bounded_signature_page",
            "signature_limit": limit, "completeness": "unknown",
            "freshness_basis": "local_collected_at; source block_time may be absent",
            "collected_at": collected_at, "cursor_advanced": False,
            "invalid_signature_records": invalid_signature_records,
            "transactions_unavailable": failed,
        }
    incomplete = failed > 0 or invalid_signature_records > 0
    if invalid_signature_records:
        reason = "invalid_signature_record"
    else:
        reason = "transaction_fetch_partial" if failed else None
    page_completeness = (
        "partial" if incomplete else "empty_page" if not signatures else "current_page_processed"
    )
    return {
        "status": "partial" if incomplete else "ok",
        "reason": reason,
        "wallet": wallet,
        "source": COLLECTION_SOURCE,
        "collection_scope": "bounded_signature_page",
        "signature_limit": limit,
        "completeness": page_completeness,
        "freshness_basis": "local_collected_at; source block_time may be absent",
        "collected_at": collected_at,
        "cursor_advanced": cursor_advanced,
        "signatures_scanned": len(signatures),
        "invalid_signature_records": invalid_signature_records,
        "transactions_unavailable": failed,
        "events_detected": len(events),
        "events_inserted": inserted,
        "events": events,
        "pnl_status": "unknown_not_reconstructed",
    }
