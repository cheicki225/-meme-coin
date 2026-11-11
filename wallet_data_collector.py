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
DEFAULT_DB_PATH = Path("data/wallet_intelligence.sqlite3")


def _account_key(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        key = value.get("pubkey")
        return key if isinstance(key, str) else None
    return None


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
                target[mint] = target.get(mint, 0) + int(amount)
                if mint in decimals and decimals[mint] != int(dec):
                    continue
                decimals[mint] = int(dec)
            except (TypeError, ValueError):
                continue
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


def normalize_transaction(wallet: str, signature: str, tx: dict) -> dict | None:
    """Normalise un mouvement observé; ne prétend pas identifier un swap certain."""
    if not isinstance(tx, dict) or not isinstance(tx.get("meta"), dict):
        return None
    meta = tx["meta"]
    if meta.get("err") is not None:
        return None
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
    pump_fun = wallet_history._transaction_involves_program(tx, config.PUMP_FUN_PROGRAM_ID)
    if not pump_fun or not token_deltas:
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
            "source": "solana_rpc_pump_fun_balance_deltas",
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
        db.execute("CREATE INDEX IF NOT EXISTS idx_wallet_activity_time ON wallet_activity(wallet_address, block_time)")
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
    rows = [
        (
            e["signature"], e["wallet_address"], e["token_mint"], e.get("block_time"),
            e["event_type"], str(e["token_delta_raw"]), e.get("token_decimals"),
            e.get("sol_delta"), e["classification_confidence"], None, None, None,
            e["source"], int(time.time()),
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


def read_wallet_activity(wallet: str, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict]:
    if not Path(db_path).exists():
        return []
    db = _connect(db_path)
    try:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM wallet_activity WHERE wallet_address=? ORDER BY block_time DESC",
            (wallet,),
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
    if not config.HELIUS_API_KEY:
        return {"status": "unavailable", "reason": "helius_not_configured", "wallet": wallet}
    cursor = get_collection_cursor(wallet, db_path)
    try:
        signatures = await wallet_history._get_signatures(wallet, limit=limit, until=cursor)
    except Exception as exc:
        log.warning("Wallet signature collection failed: %s", exc)
        return {"status": "error", "reason": "signature_fetch_failed", "wallet": wallet}
    if not isinstance(signatures, list):
        return {"status": "error", "reason": "invalid_signature_response", "wallet": wallet}

    events, failed = [], 0
    for item in signatures:
        signature = item.get("signature") if isinstance(item, dict) else None
        if not signature:
            continue
        try:
            tx = await wallet_history._get_raw_transaction(signature)
        except Exception:
            failed += 1
            continue
        if not tx:
            failed += 1
            continue
        normalized = normalize_transaction(wallet, signature, tx)
        if normalized:
            events.extend(normalized["events"])

    try:
        inserted = store_events(events, db_path)
        if failed == 0 and signatures:
            newest = signatures[0].get("signature") if isinstance(signatures[0], dict) else None
            if newest:
                set_collection_cursor(wallet, newest, db_path)
    except (OSError, sqlite3.Error) as exc:
        log.exception("Wallet activity persistence failed")
        return {"status": "error", "reason": "storage_failed", "wallet": wallet}
    return {
        "status": "partial" if failed else "ok",
        "wallet": wallet,
        "signatures_scanned": len(signatures),
        "transactions_unavailable": failed,
        "events_detected": len(events),
        "events_inserted": inserted,
        "events": events,
        "pnl_status": "unknown_not_reconstructed",
    }
