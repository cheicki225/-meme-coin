"""
════════════════════════════════════════════════════════════════
JUPITER EXECUTOR — Achats/ventes réels via l'agrégateur Jupiter
════════════════════════════════════════════════════════════════
Flux pour un swap réel :
1. Quote  : demande à Jupiter le meilleur itinéraire (prix, slippage estimé)
2. Swap   : Jupiter construit une transaction non signée
3. Signe  : le bot signe localement avec le keypair (la clé privée ne quitte
            jamais ce process)
4. Envoie : soumission à la blockchain via RPC (Helius/Alchemy)
5. Confirme : poll jusqu'à confirmation ou timeout

Toute transaction échouée lève une exception avec le détail — jamais de
silence. Le montant réellement reçu/dépensé est lu depuis la réponse Jupiter,
pas recalculé côté client (pour refléter le vrai slippage exécuté).
"""

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
        impact = float(quote.get("priceImpactPct", 0)) * 100
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


def _validate_unsigned_transaction(raw_tx: bytes, keypair: Keypair,
                                   input_mint: str, amount_lamports: int) -> tuple[VersionedTransaction, str]:
    """Vérifie le payeur, les signataires et les programmes avant toute signature."""
    try:
        tx = VersionedTransaction.from_bytes(raw_tx)
        message = tx.message
        keys = list(message.account_keys)
        required = message.header.num_required_signatures
        if not keys or str(keys[0]) != str(keypair.pubkey()):
            raise SwapError("Le payeur de la transaction ne correspond pas au wallet actif.")
        if required != 1 or str(keys[0]) != str(keypair.pubkey()):
            raise SwapError("La transaction demande des signataires supplémentaires non autorisés.")
        # Les programmes de route sont appelés en CPI par Jupiter. Les seules
        # instructions de premier niveau permises sont Jupiter et les utilitaires
        # Solana nécessaires à la préparation du swap.
        allowed_programs = {
            "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",  # Jupiter v6
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
            # Les index du message v0 peuvent viser des comptes résolus via
            # Address Lookup Tables. Ne résous les index que pour les rares
            # instructions top-level dont les comptes sont effectivement inspectés.
            accounts = []
            if program_id == "11111111111111111111111111111111" or program_id in token_programs:
                try:
                    accounts = [keys[index] for index in instruction.accounts]
                except IndexError as exc:
                    raise SwapError("Compte d'instruction non résolu; transaction refusée.") from exc
            if program_id == "11111111111111111111111111111111":
                # Le seul transfert System top-level admis finance le compte
                # WSOL propre au wallet; un transfert vers un tiers échoue fermé.
                tag = int.from_bytes(data[:4], "little") if len(data) >= 4 else -1
                if (input_mint != config.SOL_MINT or tag != 2 or len(data) < 12 or len(accounts) < 2
                        or str(accounts[0]) != str(owner)
                        or accounts[1] != wsol_ata
                        or int.from_bytes(data[4:12], "little") > amount_lamports):
                    raise SwapError("Instruction de transfert SOL non autorisée dans la transaction Jupiter.")
            if program_id in token_programs:
                # Les swaps exécutent les transferts de tokens en CPI Jupiter.
                # Au niveau supérieur, n'autoriser que la fermeture du compte
                # WSOL vers le wallet ou sa synchronisation native.
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


async def get_quote(input_mint: str, output_mint: str, amount_lamports: int, slippage_bps: int) -> dict:
    """Demande un devis Jupiter. amount_lamports est en plus petite unité du token source."""
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_lamports),
        "slippageBps": str(slippage_bps),
    }
    async with aiohttp.ClientSession(connector=rpc_client.get_http_connector(), connector_owner=False) as session:
        async with session.get(config.JUPITER_QUOTE_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise SwapError(f"Jupiter quote a échoué ({resp.status}): {text[:200]}")
            return await resp.json()


async def _build_swap_transaction(quote: dict, user_pubkey: str) -> str:
    """Demande à Jupiter de construire la transaction de swap (non signée, encodée base64)."""
    body = {
        "quoteResponse": quote,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "prioritizationFeeLamports": config.JUPITER_PRIORITIZATION_FEE_LAMPORTS,
    }
    async with aiohttp.ClientSession(connector=rpc_client.get_http_connector(), connector_owner=False) as session:
        async with session.post(config.JUPITER_SWAP_URL, json=body, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise SwapError(f"Jupiter swap-build a échoué ({resp.status}): {text[:200]}")
            data = await resp.json()
            if "swapTransaction" not in data:
                raise SwapError(f"Réponse Jupiter inattendue : {data}")
            return data["swapTransaction"]


async def _sign_and_send(swap_transaction_b64: str, keypair: Keypair) -> str:
    """Signe localement puis soumet la transaction. Retourne la signature (hash de tx)."""
    raw_tx = base64.b64decode(swap_transaction_b64)
    tx = VersionedTransaction.from_bytes(raw_tx)

    # Re-signe le message avec notre keypair (Jupiter fournit une tx non signée)
    signed_tx = VersionedTransaction(tx.message, [keypair])
    signed_bytes = bytes(signed_tx)
    signed_b64 = base64.b64encode(signed_bytes).decode("utf-8")

    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "sendTransaction",
        "params": [signed_b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
    }
    result = await rpc_client.rpc_post(payload, timeout=15)
    if not result or not isinstance(result, str):
        raise SwapError(f"Échec de l'envoi de la transaction (réponse RPC: {result})")
    return result


def _sign_transaction(swap_transaction_b64: str, keypair: Keypair):
    """Signe localement et retourne (signed_tx_bytes, signature_str, blockhash_str) sans envoyer."""
    raw_tx = base64.b64decode(swap_transaction_b64)
    tx = VersionedTransaction.from_bytes(raw_tx)
    signed_tx = VersionedTransaction(tx.message, [keypair])
    signed_bytes = bytes(signed_tx)
    signature_str = str(signed_tx.signatures[0])
    blockhash_str = str(tx.message.recent_blockhash)
    return signed_bytes, signature_str, blockhash_str


async def _send_via_jito(signed_bytes: bytes, keypair: Keypair, blockhash: str) -> None:
    """Soumet via bundle Jito (swap + tip). Lève JitoError si ça échoue — à catcher
    par l'appelant pour retomber sur l'envoi RPC classique."""
    import jito_client
    await jito_client.send_bundle_with_tip(signed_bytes, keypair, blockhash)


async def _send_via_rpc(signed_bytes: bytes) -> str:
    """Envoi classique via sendTransaction RPC."""
    signed_b64 = base64.b64encode(signed_bytes).decode("utf-8")
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "sendTransaction",
        "params": [signed_b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
    }
    result = await rpc_client.rpc_post(payload, timeout=15)
    if not result or not isinstance(result, str):
        raise SwapError(f"Échec de l'envoi de la transaction (réponse RPC: {result})")
    return result


async def _get_confirmed_transaction(signature: str, attempts: int = 5) -> dict:
    """Fetch confirmed accounting metadata; never substitute the quote for fills."""
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
        "params": [signature, {
            "encoding": "json", "commitment": "confirmed",
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
            if confirmation_status in ("confirmed", "finalized"):
                return True
        await asyncio.sleep(interval)
        elapsed += interval
    raise SwapError(f"Transaction {signature} non confirmée après {timeout_s}s (timeout).")


async def execute_swap(input_mint: str, output_mint: str, amount_lamports: int, slippage_bps: int,
                        keypair=None, use_jito: bool = False, on_signed=None) -> dict:
    """
    Exécute un swap complet réel : quote -> build -> sign -> send -> confirm.
    keypair: si fourni, signe avec ce keypair précis (utilisé par MultiBuy pour
    exécuter depuis plusieurs wallets différents). Sinon, retombe sur le wallet
    unique de wallet.py (SOLANA_PRIVATE_KEY) — comportement historique inchangé.
    use_jito: si True (et config.JITO_ENABLED), soumet via bundle Jito avec tip
    pour une inclusion plus rapide. Repli automatique et transparent sur l'envoi
    RPC classique si Jito échoue (réseau, timeout, etc.) — jamais bloquant.
    Retourne : {"signature": str, "input_amount": int, "output_amount": int,
                "price_impact_pct": float}
    Lève SwapError en cas d'échec à n'importe quelle étape.
    """
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

    quote = await get_quote(input_mint, output_mint, amount_lamports, slippage_bps)
    _validate_quote(quote, input_mint, output_mint, amount_lamports, slippage_bps)

    swap_tx_b64 = await _build_swap_transaction(quote, user_pubkey)
    try:
        raw_tx = base64.b64decode(swap_tx_b64, validate=True)
    except Exception as exc:
        raise SwapError("Transaction Jupiter encodée en base64 invalide.") from exc
    tx, blockhash = _validate_unsigned_transaction(raw_tx, keypair, input_mint, amount_lamports)
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
            "status": "submitted",
            "created_at": int(time.time()),
        }
        callback_result = on_signed(record)
        if inspect.isawaitable(callback_result):
            await callback_result

    if use_jito and config.JITO_ENABLED:
        try:
            await _send_via_jito(signed_bytes, keypair, blockhash)
            log.info(f"📡 Transaction envoyée via Jito : {signature} — confirmation en cours...")
        except Exception as e:
            log.warning(f"⚠️ Envoi Jito a échoué ({e}) — repli sur l'envoi RPC classique.")
            rpc_signature = await _send_via_rpc(signed_bytes)
            if rpc_signature != signature:
                raise SwapError("La signature renvoyée par le RPC ne correspond pas à la transaction signée.")
            log.info(f"📡 Transaction envoyée via RPC (repli) : {signature} — confirmation en cours...")
    else:
        rpc_signature = await _send_via_rpc(signed_bytes)
        if rpc_signature != signature:
            raise SwapError("La signature renvoyée par le RPC ne correspond pas à la transaction signée.")
        log.info(f"📡 Transaction envoyée : {signature} — confirmation en cours...")

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

    return {
        "signature": signature,
        "input_amount": input_amount,
        "output_amount": output_amount,
        "price_impact_pct": float(quote.get("priceImpactPct", 0)) * 100,
        "accounting": accounting,
    }
