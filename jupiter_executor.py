"""Jupiter Swap API V2 execution with local transaction validation and
confirmed, fee-inclusive on-chain accounting. Ambiguous outcomes remain
persisted as pending and are never retried automatically."""

import asyncio
import base64
import logging
import math
import inspect
import time
import re

import aiohttp
from solders.transaction import VersionedTransaction
from solders.keypair import Keypair

import config
import rpc_client
from execution_metrics import measure_async, measure_execution
from transaction_accounting import AccountingError, account_confirmed_swap
import wallet

log = logging.getLogger("jupiter_executor")
JUPITER_PROGRAM_ID = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
_JUPITER_ROUTE = bytes.fromhex("e517cb977ae3ad2a")
_JUPITER_SHARED_ACCOUNTS_ROUTE = bytes.fromhex("c1209b3341d69c81")
HARD_MAX_SLIPPAGE_BPS = 1_000  # 10%; environment configuration can only lower this ceiling.
HARD_MAX_PRICE_IMPACT_PCT = 5.0


class SwapError(Exception):
    pass


def _strict_uint(value, field: str) -> int:
    """Parse an unsigned integer without accepting bools or truncating floats."""
    if type(value) is int:
        parsed = value
    elif isinstance(value, str) and value and re.fullmatch(r"[0-9]+", value):
        parsed = int(value)
    else:
        raise SwapError(f"Champ numérique invalide dans le devis Jupiter ({field}).")
    if parsed < 0:
        raise SwapError(f"Champ numérique négatif dans le devis Jupiter ({field}).")
    return parsed


def _strict_rpc_fee_lamports(result: dict) -> int:
    """Accept only the integer shape returned by Solana getFeeForMessage."""
    if not isinstance(result, dict) or type(result.get("value")) is not int:
        raise SwapError("Frais RPC absents ou mal formés.")
    fee = result["value"]
    if fee < 0:
        raise SwapError("Frais RPC négatifs refusés.")
    return fee


def _maximum_allowed_slippage_bps() -> int:
    configured = getattr(config, "MAX_SLIPPAGE_BPS", None)
    if type(configured) is not int or configured <= 0:
        raise SwapError("MAX_SLIPPAGE_BPS must be a positive integer.")
    return min(configured, HARD_MAX_SLIPPAGE_BPS)


def _maximum_allowed_price_impact_pct() -> float:
    configured = getattr(config, "MAX_PRICE_IMPACT_PCT", None)
    if isinstance(configured, bool) or not isinstance(configured, (int, float)):
        raise SwapError("MAX_PRICE_IMPACT_PCT must be a finite non-negative number.")
    try:
        configured = float(configured)
    except (OverflowError, TypeError, ValueError) as exc:
        raise SwapError("MAX_PRICE_IMPACT_PCT must be a finite non-negative number.") from exc
    if not math.isfinite(configured) or configured < 0:
        raise SwapError("MAX_PRICE_IMPACT_PCT must be a finite non-negative number.")
    return min(configured, HARD_MAX_PRICE_IMPACT_PCT)


def _quote_price_impact_percent(quote: dict) -> float:
    """Read the documented decimal `priceImpactPct` or percent `priceImpact`."""
    impacts = []
    for field, multiplier in (("priceImpact", 1), ("priceImpactPct", 100)):
        raw_impact = quote.get(field)
        if raw_impact is None:
            continue
        if isinstance(raw_impact, bool):
            raise SwapError("L'impact de prix Jupiter est mal formé.")
        try:
            impact = abs(float(raw_impact) * multiplier)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SwapError("L'impact de prix Jupiter est mal formé.") from exc
        if not math.isfinite(impact):
            raise SwapError("L'impact de prix Jupiter n'est pas fini.")
        impacts.append(impact)
    if not impacts:
        raise SwapError("Le devis Jupiter ne contient pas d'impact de prix.")
    if len(impacts) == 2 and not math.isclose(impacts[0], impacts[1], rel_tol=1e-6, abs_tol=1e-6):
        raise SwapError("Les champs d'impact de prix Jupiter sont incohérents.")
    return max(impacts)


def _associated_token_address(owner, mint, token_program, associated_program):
    from solders.pubkey import Pubkey

    return Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)], associated_program
    )[0]


def _validate_jupiter_swap_instruction(
    data: bytes,
    accounts: list,
    *,
    owner,
    input_mint,
    output_mint,
    expected_in_amount: int,
    expected_out_amount: int,
    slippage_bps: int,
) -> None:
    """Bind supported exact-in Jupiter v6 route arguments to the checked order.

    The supported layouts come from Jupiter's published v6 IDL. Route-plan
    variants are still executed by CPI and are not independently audited here.
    """
    from solders.pubkey import Pubkey

    if len(data) < 8 + 4 + 4 + 19:
        raise SwapError("Instruction Jupiter de swap vide ou tronquée.")
    discriminator = data[:8]
    if discriminator == _JUPITER_ROUTE:
        route_count_offset = 8
        source_account_index, destination_account_index = 2, 3
        authority_index, output_mint_index = 1, 5
        input_mint_index = None
        token_program_index = 0
        minimum_route_prefix = 8 + 4
    elif discriminator == _JUPITER_SHARED_ACCOUNTS_ROUTE:
        route_count_offset = 9
        source_account_index, destination_account_index = 3, 6
        authority_index, output_mint_index = 2, 8
        input_mint_index = 7
        token_program_index = 0
        minimum_route_prefix = 9 + 4
    else:
        raise SwapError("Instruction Jupiter non prise en charge (route exact-in attendue).")

    if len(accounts) <= max(source_account_index, destination_account_index, authority_index,
                            output_mint_index, token_program_index):
        raise SwapError("Comptes requis de l'instruction Jupiter manquants.")
    if accounts[authority_index] != owner:
        raise SwapError("L'autorité de transfert Jupiter n'est pas le wallet actif.")
    if accounts[output_mint_index] != output_mint:
        raise SwapError("Le mint de sortie de l'instruction Jupiter ne correspond pas au devis.")
    if input_mint_index is not None and accounts[input_mint_index] != input_mint:
        raise SwapError("Le mint d'entrée de l'instruction Jupiter ne correspond pas au devis.")

    token_programs = {
        Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"),
        Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"),
    }
    token_program = accounts[token_program_index]
    if token_program not in token_programs:
        raise SwapError("Programme SPL d'entrée Jupiter non autorisé.")
    associated_program = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
    expected_source = _associated_token_address(owner, input_mint, token_program, associated_program)
    if accounts[source_account_index] != expected_source:
        raise SwapError("Le compte source Jupiter n'est pas l'ATA du wallet et du mint demandés.")

    output_token_program = token_program
    if discriminator == _JUPITER_SHARED_ACCOUNTS_ROUTE and len(accounts) > 10:
        optional_token_program = accounts[10]
        if optional_token_program in token_programs:
            output_token_program = optional_token_program
    expected_destination = _associated_token_address(
        owner, output_mint, output_token_program, associated_program
    )
    if accounts[destination_account_index] != expected_destination:
        raise SwapError("Le compte destination Jupiter n'est pas l'ATA du wallet et du mint demandés.")

    if len(data) < minimum_route_prefix + 4 + 19:
        raise SwapError("Route plan Jupiter incomplet.")
    route_count = int.from_bytes(data[route_count_offset:route_count_offset + 4], "little")
    route_plan_start = route_count_offset + 4
    route_plan_end = len(data) - 19
    if route_count < 1 or route_count > 64 or route_plan_end < route_plan_start + route_count * 4:
        raise SwapError("Route plan Jupiter invalide.")

    # Both published exact-in layouts end in inAmount, quotedOutAmount,
    # slippageBps, platformFeeBps, independent of the variable route plan.
    input_amount = int.from_bytes(data[-19:-11], "little")
    quoted_output = int.from_bytes(data[-11:-3], "little")
    instruction_slippage = int.from_bytes(data[-3:-1], "little")
    platform_fee_bps = data[-1]
    if input_amount != expected_in_amount or quoted_output != expected_out_amount:
        raise SwapError("Les montants de l'instruction Jupiter ne correspondent pas au devis vérifié.")
    if instruction_slippage != slippage_bps:
        raise SwapError("Le slippage de l'instruction Jupiter ne correspond pas au devis vérifié.")
    if platform_fee_bps != 0:
        raise SwapError("Les frais de plateforme Jupiter ne sont pas autorisés.")


def _validate_quote(quote: dict, input_mint: str, output_mint: str,
                    amount_lamports: int, slippage_bps: int) -> None:
    """Refuse les devis incohérents ou au-delà des limites de risque locales."""
    if not isinstance(quote, dict):
        raise SwapError("Devis Jupiter invalide (objet attendu).")
    if quote.get("inputMint") != input_mint or quote.get("outputMint") != output_mint:
        raise SwapError("Le devis Jupiter ne correspond pas aux mints demandés.")
    try:
        quoted_in = _strict_uint(quote["inAmount"], "inAmount")
        quoted_out = _strict_uint(quote["outAmount"], "outAmount")
        threshold = _strict_uint(quote["otherAmountThreshold"], "otherAmountThreshold")
        impact = _quote_price_impact_percent(quote)
    except (KeyError, TypeError, ValueError, OverflowError, SwapError) as exc:
        raise SwapError("Devis Jupiter incomplet ou mal formé.") from exc
    if (type(amount_lamports) is not int or amount_lamports <= 0
            or quoted_in != amount_lamports or quoted_out <= 0):
        raise SwapError("Montants du devis Jupiter invalides ou inattendus.")
    maximum_slippage = _maximum_allowed_slippage_bps()
    if type(slippage_bps) is not int or not 0 < slippage_bps <= maximum_slippage:
        raise SwapError(f"Slippage demandé hors limite (maximum {maximum_slippage} bps).")
    try:
        if _strict_uint(quote["slippageBps"], "slippageBps") != slippage_bps:
            raise SwapError("Le slippage du devis ne correspond pas à la demande.")
    except (KeyError, TypeError, ValueError, SwapError) as exc:
        raise SwapError("Le devis Jupiter ne précise pas son slippage.") from exc
    if not 0 < threshold <= quoted_out:
        raise SwapError("Minimum reçu du devis Jupiter invalide.")
    expected_minimum = (quoted_out * (10_000 - slippage_bps)) // 10_000
    if threshold != expected_minimum:
        raise SwapError("Le minimum reçu du devis n'est pas imposé par l'instruction Jupiter validée.")
    maximum_impact = _maximum_allowed_price_impact_pct()
    if not math.isfinite(impact) or impact < 0 or impact > maximum_impact:
        raise SwapError(f"Impact de prix hors limite ({impact:g}% ; maximum {maximum_impact:g}%).")


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
    expected_out_amount: int = None,
    slippage_bps: int = None,
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
            JUPITER_PROGRAM_ID,
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

        jupiter_swap_count = 0
        for instruction in message.instructions:
            try:
                program_id = str(keys[instruction.program_id_index])
            except (IndexError, AttributeError) as exc:
                raise SwapError("Instruction de transaction mal formée.") from exc
            if program_id not in allowed_programs:
                raise SwapError(f"Programme de transaction non autorisé: {program_id}.")
            is_jupiter = program_id == JUPITER_PROGRAM_ID
            data = bytes(instruction.data)
            accounts = []
            if (program_id == "11111111111111111111111111111111"
                    or program_id in token_programs
                    or program_id == str(associated_program)
                    or is_jupiter):
                try:
                    accounts = [keys[index] for index in instruction.accounts]
                except IndexError as exc:
                    raise SwapError("Compte d'instruction non résolu; transaction refusée.") from exc

            if is_jupiter:
                jupiter_swap_count += 1
                if jupiter_swap_count > 1:
                    raise SwapError("Plus d'une instruction de swap Jupiter dans une même transaction est refusée.")
                if (output_mint is None or type(expected_out_amount) is not int
                        or type(slippage_bps) is not int):
                    raise SwapError("Les attentes du devis manquent pour valider l'instruction Jupiter.")
                _validate_jupiter_swap_instruction(
                    data,
                    accounts,
                    owner=owner,
                    input_mint=Pubkey.from_string(input_mint),
                    output_mint=Pubkey.from_string(output_mint),
                    expected_in_amount=amount_lamports,
                    expected_out_amount=expected_out_amount,
                    slippage_bps=slippage_bps,
                )

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

        if jupiter_swap_count != 1:
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
        fee = _strict_rpc_fee_lamports(result)
    except SwapError as exc:
        raise SwapError("Frais de transaction impossibles à vérifier; transaction non signée.") from exc
    if fee > config.MAX_TRANSACTION_FEE_LAMPORTS:
        raise SwapError(f"Frais réseau ({fee} lamports) supérieurs à la limite configurée.")


@measure_async("jupiter.order", "jupiter")
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
            # The x-api-key must never be forwarded to an untrusted redirect.
            allow_redirects=False,
        ) as resp:
            if resp.status != 200:
                raise SwapError(f"Jupiter order a échoué (HTTP {resp.status}).")
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
    if order.get("gasless") is not False:
        raise SwapError("Les swaps sponsorisés sont désactivés.")
    if not isinstance(order.get("requestId"), str) or not order["requestId"]:
        raise SwapError("L'ordre Jupiter ne contient pas de requestId.")
    try:
        last_valid_height = _strict_uint(order.get("lastValidBlockHeight"), "lastValidBlockHeight")
        rent = _strict_uint(order.get("rentFeeLamports", 0), "rentFeeLamports")
    except SwapError as exc:
        raise SwapError("Les frais ou la validité de l'ordre Jupiter sont mal formés.") from exc
    if last_valid_height <= 0:
        raise SwapError("La hauteur d'expiration Jupiter est invalide.")
    if rent < 0 or rent > config.MAX_JUPITER_RENT_LAMPORTS:
        raise SwapError("Le loyer de création des comptes dépasse la limite configurée.")
    for field in ("signatureFeePayer", "prioritizationFeePayer", "rentFeePayer"):
        payer = order.get(field)
        if payer not in (None, "", user_pubkey):
            raise SwapError(f"Payeur tiers non autorisé dans l'ordre Jupiter ({field}).")


@measure_async("jupiter.build", "jupiter")
async def _build_swap_transaction(order: dict, user_pubkey: str) -> str:
    """Return the v0 transaction from an already validated V2 order."""
    _validate_order(order, user_pubkey)
    transaction = order.get("transaction")
    if not isinstance(transaction, str) or not transaction:
        raise SwapError("Jupiter n'a pas fourni de transaction signable.")
    return transaction


@measure_async("jupiter.submit", "jupiter")
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
            # A redirect could disclose the signed transaction and API key.
            allow_redirects=False,
        ) as resp:
            if resp.status != 200:
                raise SwapError(f"Jupiter execute a échoué (HTTP {resp.status}).")
            data = await resp.json()
    if not isinstance(data, dict):
        raise SwapError("Réponse Jupiter execute invalide.")
    if data.get("signature") != signature:
        raise SwapError("La signature de Jupiter ne correspond pas à la transaction signée.")
    if data.get("status") != "Success":
        raise SwapError(f"Jupiter a signalé un échec d'exécution ({data.get('code', 'inconnu')}).")
    return data


@measure_async("jupiter.accounting_fetch", "solana_rpc")
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


@measure_async("jupiter.confirm", "solana_rpc")
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


@measure_async("jupiter.execute_swap", "jupiter")
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
        expected_out_amount=_strict_uint(order["outAmount"], "outAmount"),
        slippage_bps=slippage_bps,
        resolved_lookup_addresses=resolved_lookup_addresses,
    )
    await _assert_blockhash_valid(blockhash)
    await _assert_fee_within_limit(tx)
    with measure_execution("jupiter.sign", "local"):
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
        "price_impact_pct": _quote_price_impact_percent(order),
        "accounting": accounting,
    }
