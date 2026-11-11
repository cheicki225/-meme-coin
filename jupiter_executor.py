"""Jupiter Swap API V2 execution with local transaction validation and
confirmed, fee-inclusive on-chain accounting. Ambiguous outcomes remain
persisted as pending and are never retried automatically."""

import asyncio
import base64
import logging
import math
import inspect
import time

import aiohttp
from solders.transaction import VersionedTransaction
from solders.keypair import Keypair

import config
import rpc_client
from transaction_accounting import AccountingError, account_confirmed_swap
import wallet

log = logging.getLogger("jupiter_executor")


class SwapError(Exception):
    pass


def _validate_quote(quote: dict, input_mint: str, output_mint: str,
                    amount_lamports: int, slippage_bps: int) -> None:
    """Refuse les devis incohérents ou au-delà des limites de risque locales."""
    if not isinstance(quote, dict):
        raise SwapError("Devis Jupiter invalide (objet attendu).")
    if quote.get("inputMint") != input_mint or quote.get("outputMint") != output_mint:
        raise SwapError("Le devis Jupiter ne correspond pas aux mints demandés.")
    try:
        quoted_in = int(quote["inAmount"])
        quoted_out = int(quote["outAmount"])
        threshold = int(quote["otherAmountThreshold"])
        if quote.get("priceImpact") is not None:
            impact = abs(float(quote["priceImpact"]))
        else:
            impact = abs(float(quote.get("priceImpactPct", 0)) * 100)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise SwapError("Devis Jupiter incomplet ou mal formé.") from exc
    if amount_lamports <= 0 or quoted_in != amount_lamports or quoted_out <= 0:
        raise SwapError("Montants du devis Jupiter invalides ou inattendus.")
    if not 0 < slippage_bps <= config.MAX_SLIPPAGE_BPS:
        raise SwapError(f"Slippage demandé hors limite (maximum {config.MAX_SLIPPAGE_BPS} bps).")
    try:
        if int(quote["slippageBps"]) != slippage_bps:
            raise SwapError("Le slippage du devis ne correspond pas à la demande.")
    except (KeyError, TypeError, ValueError) as exc:
        raise SwapError("Le devis Jupiter ne précise pas son slippage.") from exc
    if not 0 < threshold <= quoted_out:
        raise SwapError("Minimum reçu du devis Jupiter invalide.")
    if not math.isfinite(impact) or impact < 0 or impact > config.MAX_PRICE_IMPACT_PCT:
        raise SwapError(f"Impact de prix hors limite ({impact:g}% ; maximum {config.MAX_PRICE_IMPACT_PCT:g}%).")


async def _resolve_lookup_addresses(message) -> tuple[list, list]:
    """Resolve every v0 address lookup table through RPC before instruction inspection."""
    lookups = getattr(message, "address_table_lookups", ())
    if not lookups:
        return [], []

    from solders.address_lookup_table_account import ADDRESS_LOOKUP_TABLE_ID, AddressLookupTable

    writable_addresses = []
    readonly_addresses = []
    for lookup in lookups:
        table_key = str(lookup.account_key)
        result = await rpc_client.rpc_post({
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [table_key, {"encoding": "base64", "commitment": "confirmed"}],
        }, timeout=10)
        value = result.get("value") if isinstance(result, dict) else None
        if not isinstance(value, dict) or value.get("owner") != str(ADDRESS_LOOKUP_TABLE_ID):
            raise SwapError("Address lookup table absent or owned by an unexpected program.")
        encoded = value.get("data")
        if isinstance(encoded, list):
            if not encoded or (len(encoded) > 1 and encoded[1] != "base64"):
                raise SwapError("Address lookup table encoding is invalid.")
            encoded = encoded[0]
        if not isinstance(encoded, str):
            raise SwapError("Address lookup table data is unavailable.")
        try:
            table_data = base64.b64decode(encoded, validate=True)
            table = AddressLookupTable.deserialize(table_data)
            addresses = list(table.addresses)
            for index in lookup.writable_indexes:
                if index >= len(addresses):
                    raise SwapError("Address lookup table writable index is out of range.")
                writable_addresses.append(addresses[index])
            for index in lookup.readonly_indexes:
                if index >= len(addresses):
                    raise SwapError("Address lookup table readonly index is out of range.")
                readonly_addresses.append(addresses[index])
        except SwapError:
            raise
        except Exception as exc:
            raise SwapError("Address lookup table could not be decoded safely.") from exc

    return writable_addresses, readonly_addresses


def _validate_unsigned_transaction(
    raw_tx: bytes,
    keypair: Keypair,
    input_mint: str,
    amount_lamports: int,
    output_mint: str = None,
    resolved_lookup_addresses: tuple[list, list] = None,
) -> tuple[VersionedTransaction, str]:
    """Validate the payer, signers, lookup tables, and every top-level program."""
    try:
        tx = VersionedTransaction.from_bytes(raw_tx)
        message = tx.message
        lookups = getattr(message, "address_table_lookups", ())
        if lookups and resolved_lookup_addresses is None:
            raise SwapError("Address lookup table indexes must be resolved before signing.")
        loaded_writable, loaded_readonly = resolved_lookup_addresses or ([], [])
        expected_writable = sum(len(item.writable_indexes) for item in lookups)
        expected_readonly = sum(len(item.readonly_indexes) for item in lookups)
        if len(loaded_writable) != expected_writable or len(loaded_readonly) != expected_readonly:
            raise SwapError("Address lookup table resolution is incomplete.")
        keys = list(message.account_keys) + list(loaded_writable) + list(loaded_readonly)
        required = message.header.num_required_signatures
        if not keys or str(keys[0]) != str(keypair.pubkey()):
            raise SwapError("Le payeur de la transaction ne correspond pas au wallet actif.")
        if required != 1 or str(keys[0]) != str(keypair.pubkey()):
            raise SwapError("La transaction demande des signataires supplémentaires non autorisés.")

        allowed_programs = {
            "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
            "ComputeBudget111111111111111111111111111111",
            "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
            "11111111111111111111111111111111",
            "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
        }
        from solders.pubkey import Pubkey

        owner = keypair.pubkey()
        token_programs = {
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
        }
        associated_program = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
        token_program = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
        system_program = Pubkey.from_string("11111111111111111111111111111111")
        allowed_mints = {Pubkey.from_string(input_mint)}
        if output_mint:
            allowed_mints.add(Pubkey.from_string(output_mint))
        wsol_mint = Pubkey.from_string(config.SOL_MINT)
        wsol_ata = Pubkey.find_program_address(
            [bytes(owner), bytes(token_program), bytes(wsol_mint)], associated_program
        )[0]

        has_jupiter_swap = False
        for instruction in message.instructions:
            try:
                program_id = str(keys[instruction.program_id_index])
            except (IndexError, AttributeError) as exc:
                raise SwapError("Instruction de transaction mal formée.") from exc
            if program_id not in allowed_programs:
                raise SwapError(f"Programme de transaction non autorisé: {program_id}.")
            has_jupiter_swap |= program_id == "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
            data = bytes(instruction.data)
            accounts = []
            if (program_id == "11111111111111111111111111111111"
                    or program_id in token_programs
                    or program_id == str(associated_program)):
                try:
                    accounts = [keys[index] for index in instruction.accounts]
                except IndexError as exc:
                    raise SwapError("Compte d'instruction non résolu; transaction refusée.") from exc

            if program_id == "11111111111111111111111111111111":
                tag = int.from_bytes(data[:4], "little") if len(data) >= 4 else -1
                if (input_mint != config.SOL_MINT or tag != 2 or len(data) < 12 or len(accounts) < 2
                        or accounts[0] != owner or accounts[1] != wsol_ata
                        or int.from_bytes(data[4:12], "little") > amount_lamports):
                    raise SwapError("Instruction de transfert SOL non autorisée dans la transaction Jupiter.")

            if program_id == str(associated_program):
                if len(data) not in (0, 1) or data not in (b"", b"\x01") or len(accounts) < 6:
                    raise SwapError("Instruction de création de compte associé non autorisée.")
                mint = accounts[3]
                token_program_id = str(accounts[5])
                expected_ata = Pubkey.find_program_address(
                    [bytes(owner), bytes(accounts[5]), bytes(mint)], associated_program
                )[0]
                if (accounts[0] != owner or accounts[1] != expected_ata or accounts[2] != owner
                        or mint not in allowed_mints or accounts[4] != system_program
                        or token_program_id not in token_programs):
                    raise SwapError("Création d'ATA pour un autre wallet ou mint refusée.")

            if program_id in token_programs:
                tag = data[0] if data else -1
                close_ok = (
                    tag == 9 and len(accounts) >= 3 and accounts[1] == owner
                    and accounts[2] == owner and accounts[0] == wsol_ata
                )
                sync_ok = tag == 17 and len(accounts) == 1 and accounts[0] == wsol_ata
                if not (close_ok or sync_ok):
                    raise SwapError("Instruction SPL top-level non autorisée dans la transaction Jupiter.")

        if not has_jupiter_swap:
            raise SwapError("La transaction ne contient pas d'instruction de swap Jupiter v6.")
        return tx, str(message.recent_blockhash)
    except SwapError:
        raise
    except Exception as exc:
        raise SwapError("Transaction Jupiter illisible ou invalide.") from exc


async def _assert_blockhash_valid(blockhash: str) -> None:
    result = await rpc_client.rpc_post({
        "jsonrpc": "2.0", "id": 1, "method": "isBlockhashValid",
        "params": [blockhash, {"commitment": "confirmed"}],
    }, timeout=10)
    if not isinstance(result, dict) or result.get("value") is not True:
        raise SwapError("Blockhash absent ou expiré; transaction non signée.")


async def _assert_fee_within_limit(tx: VersionedTransaction) -> None:
    message_b64 = base64.b64encode(bytes(tx.message)).decode("ascii")
    result = await rpc_client.rpc_post({
        "jsonrpc": "2.0", "id": 1, "method": "getFeeForMessage",
        "params": [message_b64, {"encoding": "base64", "commitment": "confirmed"}],
    }, timeout=10)
    try:
        fee = int(result["value"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SwapError("Frais de transaction impossibles à vérifier; transaction non signée.") from exc
    if fee < 0 or fee > config.MAX_TRANSACTION_FEE_LAMPORTS:
        raise SwapError(f"Frais réseau ({fee} lamports) supérieurs à la limite configurée.")


async def get_quote(input_mint: str, output_mint: str, amount_lamports: int,
                    slippage_bps: int, user_pubkey: str = None) -> dict:
    """Get a Swap API V2 order; the returned transaction remains unsigned."""
    api_key = getattr(config, "JUPITER_API_KEY", "")
    if not isinstance(api_key, str) or not api_key.strip():
        raise SwapError("JUPITER_API_KEY is required for Jupiter Swap API V2.")
    if not user_pubkey:
        raise SwapError("A taker wallet is required to build a Jupiter order.")
    fee_cap = min(
        int(config.MAX_TRANSACTION_FEE_LAMPORTS),
        int(config.JUPITER_PRIORITIZATION_FEE_LAMPORTS),
    )
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_lamports),
        "taker": user_pubkey,
        "slippageBps": str(slippage_bps),
        "priorityFeeLamports": str(fee_cap),
        "broadcastFeeType": "maxCap",
        "excludeRouters": "jupiterz,dflow,okx",
        "maxSupportedTransactionVersion": "0",
    }
    headers = {"x-api-key": api_key}
    async with aiohttp.ClientSession(connector=rpc_client.get_http_connector(), connector_owner=False) as session:
        async with session.get(
            config.JUPITER_ORDER_URL,
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise SwapError(f"Jupiter order a échoué ({resp.status}): {text[:200]}")
            data = await resp.json()
            if not isinstance(data, dict):
                raise SwapError("Réponse Jupiter order invalide.")
            if data.get("error"):
                raise SwapError("Jupiter order a refusé la demande.")
            return data


def _validate_order(order: dict, user_pubkey: str) -> None:
    """Enforce single-wallet Metis routing and reject sponsored/third-party fees."""
    if not isinstance(order, dict):
        raise SwapError("Réponse Jupiter order invalide.")
    if order.get("taker") != user_pubkey:
        raise SwapError("Le taker de l'ordre Jupiter ne correspond pas au wallet actif.")
    if order.get("router") != "metis":
        raise SwapError("Routeur Jupiter non autorisé; seul Metis est accepté.")
    if order.get("gasless") is True:
        raise SwapError("Les swaps sponsorisés sont désactivés.")
    if not isinstance(order.get("requestId"), str) or not order["requestId"]:
        raise SwapError("L'ordre Jupiter ne contient pas de requestId.")
    if not isinstance(order.get("lastValidBlockHeight"), (str, int)):
        raise SwapError("L'ordre Jupiter ne contient pas de durée de validité.")
    try:
        last_valid_height = int(order["lastValidBlockHeight"])
        rent = int(order.get("rentFeeLamports", 0) or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SwapError("Les frais ou la validité de l'ordre Jupiter sont mal formés.") from exc
    if last_valid_height <= 0:
        raise SwapError("La hauteur d'expiration Jupiter est invalide.")
    if rent < 0 or rent > config.MAX_JUPITER_RENT_LAMPORTS:
        raise SwapError("Le loyer de création des comptes dépasse la limite configurée.")
    for field in ("signatureFeePayer", "prioritizationFeePayer", "rentFeePayer"):
        payer = order.get(field)
        if payer not in (None, "", user_pubkey):
            raise SwapError(f"Payeur tiers non autorisé dans l'ordre Jupiter ({field}).")


async def _build_swap_transaction(order: dict, user_pubkey: str) -> str:
    """Return the v0 transaction from an already validated V2 order."""
    _validate_order(order, user_pubkey)
    transaction = order.get("transaction")
    if not isinstance(transaction, str) or not transaction:
        raise SwapError("Jupiter n'a pas fourni de transaction signable.")
    return transaction


async def _execute_order(order: dict, signed_bytes: bytes, signature: str) -> dict:
    """Submit one signed order through Jupiter's managed Swap API V2 landing."""
    api_key = getattr(config, "JUPITER_API_KEY", "")
    if not isinstance(api_key, str) or not api_key.strip():
        raise SwapError("JUPITER_API_KEY is required for Jupiter Swap API V2.")
    payload = {
        "signedTransaction": base64.b64encode(signed_bytes).decode("ascii"),
        "requestId": order["requestId"],
        "lastValidBlockHeight": str(order["lastValidBlockHeight"]),
    }
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    async with aiohttp.ClientSession(connector=rpc_client.get_http_connector(), connector_owner=False) as session:
        async with session.post(
            config.JUPITER_EXECUTE_URL,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise SwapError(f"Jupiter execute a échoué ({resp.status}): {text[:200]}")
            data = await resp.json()
    if not isinstance(data, dict):
        raise SwapError("Réponse Jupiter execute invalide.")
    if data.get("signature") != signature:
        raise SwapError("La signature de Jupiter ne correspond pas à la transaction signée.")
    if data.get("status") != "Success":
        raise SwapError(f"Jupiter a signalé un échec d'exécution ({data.get('code', 'inconnu')}).")
    return data


async def _get_confirmed_transaction(signature: str, attempts: int = 5) -> dict:
    """Fetch confirmed accounting metadata; never substitute the quote for fills."""
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
        "params": [signature, {
            "encoding": "json", "commitment": "finalized",
            "maxSupportedTransactionVersion": 0,
        }],
    }
    for attempt in range(attempts):
        result = await rpc_client.rpc_post(payload, timeout=15)
        if isinstance(result, dict) and isinstance(result.get("meta"), dict):
            return result
        if attempt + 1 < attempts:
            await asyncio.sleep(min(2 * (attempt + 1), 6))
    raise SwapError(
        f"Transaction {signature} was confirmed but accounting metadata is unavailable; "
        "the pending signature is retained for manual reconciliation."
    )


async def _confirm_transaction(signature: str, timeout_s: int = None) -> bool:
    """Poll jusqu'à confirmation de la transaction ou timeout."""
    timeout_s = timeout_s or config.TX_CONFIRMATION_TIMEOUT_S
    elapsed = 0
    interval = 2
    while elapsed < timeout_s:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignatureStatuses",
            "params": [[signature], {"searchTransactionHistory": True}],
        }
        result = await rpc_client.rpc_post(payload, timeout=10)
        statuses = result.get("value", [None]) if isinstance(result, dict) else [None]
        status = statuses[0] if statuses else None
        if status:
            if status.get("err"):
                raise SwapError(f"Transaction {signature} a échoué on-chain : {status['err']}")
            confirmation_status = status.get("confirmationStatus")
            if confirmation_status == "finalized":
                return True
        await asyncio.sleep(interval)
        elapsed += interval
    raise SwapError(f"Transaction {signature} non confirmée après {timeout_s}s (timeout).")


async def execute_swap(input_mint: str, output_mint: str, amount_lamports: int, slippage_bps: int,
                        keypair=None, use_jito: bool = False, on_signed=None, on_accounted=None) -> dict:
    """
    Execute a Jupiter Swap API V2 order after local validation, signing,
    managed landing, confirmation, and fee-inclusive balance accounting.
    The use_jito argument remains for caller compatibility; no separate tip
    transaction is submitted. Ambiguous outcomes stay pending without retry.
    """
    if config.TRADING_MODE != "LIVE" or not getattr(config, "LIVE_EXECUTION_ENABLED", False):
        raise SwapError(
            "LIVE execution is disabled; PAPER remains default and LIVE needs an explicit second opt-in."
        )
    if not isinstance(amount_lamports, int) or isinstance(amount_lamports, bool) or amount_lamports <= 0:
        raise SwapError("Le montant du swap doit être un entier positif.")
    if not isinstance(slippage_bps, int) or isinstance(slippage_bps, bool):
        raise SwapError("Le slippage doit être exprimé en points de base entiers.")
    try:
        from solders.pubkey import Pubkey
        Pubkey.from_string(input_mint)
        Pubkey.from_string(output_mint)
    except Exception as exc:
        raise SwapError("Mint Solana invalide.") from exc
    if input_mint == output_mint:
        raise SwapError("Les mints source et destination doivent être différents.")
    if keypair is None:
        keypair = wallet.load_keypair()
    user_pubkey = str(keypair.pubkey())

    order = await get_quote(
        input_mint, output_mint, amount_lamports, slippage_bps, user_pubkey=user_pubkey
    )
    _validate_quote(order, input_mint, output_mint, amount_lamports, slippage_bps)
    _validate_order(order, user_pubkey)

    swap_tx_b64 = await _build_swap_transaction(order, user_pubkey)
    try:
        raw_tx = base64.b64decode(swap_tx_b64, validate=True)
    except Exception as exc:
        raise SwapError("Transaction Jupiter encodée en base64 invalide.") from exc
    unsigned_tx = VersionedTransaction.from_bytes(raw_tx)
    resolved_lookup_addresses = await _resolve_lookup_addresses(unsigned_tx.message)
    tx, blockhash = _validate_unsigned_transaction(
        raw_tx, keypair, input_mint, amount_lamports, output_mint,
        resolved_lookup_addresses=resolved_lookup_addresses,
    )
    await _assert_blockhash_valid(blockhash)
    await _assert_fee_within_limit(tx)
    signed_tx = VersionedTransaction(tx.message, [keypair])
    signed_bytes = bytes(signed_tx)
    signature = str(signed_tx.signatures[0])

    if on_signed is not None:
        record = {
            "signature": signature,
            "wallet": user_pubkey,
            "input_mint": input_mint,
            "output_mint": output_mint,
            "requested_input_lamports": amount_lamports,
            "request_id": order["requestId"],
            "status": "submitted",
            "created_at": int(time.time()),
        }
        callback_result = on_signed(record)
        if inspect.isawaitable(callback_result):
            await callback_result

    # Jito's separate tip transaction is intentionally not used. V2 /execute
    # handles landing; ambiguous network failures remain pending and are never retried here.
    await _execute_order(order, signed_bytes, signature)
    log.info(f"📡 Transaction soumise via Jupiter Swap API V2 : {signature}")
    await _confirm_transaction(signature)
    log.info(f"✅ Transaction confirmée : {signature}")

    try:
        confirmed = await _get_confirmed_transaction(signature)
        accounting = account_confirmed_swap(user_pubkey, input_mint, output_mint, confirmed)
    except AccountingError as exc:
        raise SwapError(
            f"Transaction {signature} is confirmed but its balance deltas are unsafe to account: {exc}"
        ) from exc

    input_delta = accounting.get("input_delta_raw")
    output_delta = accounting.get("output_delta_raw")
    if input_mint == config.SOL_MINT:
        input_amount = max(0, -accounting["native_delta_lamports"])
    else:
        input_amount = abs(int(input_delta or 0))
    if output_mint == config.SOL_MINT:
        output_amount = max(0, accounting["native_delta_lamports"])
    else:
        output_amount = max(0, int(output_delta or 0))
    if input_amount <= 0 or output_amount <= 0:
        raise SwapError(
            f"Transaction {signature} is confirmed but actual swap amounts are unavailable; "
            "pending state must be reconciled manually."
        )

    if on_accounted is not None:
        callback_result = on_accounted({"signature": signature, "accounting": accounting})
        if inspect.isawaitable(callback_result):
            await callback_result

    return {
        "signature": signature,
        "input_amount": input_amount,
        "output_amount": output_amount,
        "price_impact_pct": abs(float(order.get("priceImpact", float(order.get("priceImpactPct", 0)) * 100))),
        "accounting": accounting,
    }
