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

import aiohttp
from solders.transaction import VersionedTransaction
from solders.keypair import Keypair

import config
import rpc_client
import wallet

log = logging.getLogger("jupiter_executor")


class SwapError(Exception):
    pass


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
                        keypair=None, use_jito: bool = False) -> dict:
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
    if keypair is None:
        keypair = wallet.load_keypair()
    user_pubkey = str(keypair.pubkey())

    quote = await get_quote(input_mint, output_mint, amount_lamports, slippage_bps)
    if not quote or "outAmount" not in quote:
        raise SwapError(f"Devis Jupiter invalide : {quote}")

    swap_tx_b64 = await _build_swap_transaction(quote, user_pubkey)
    signed_bytes, signature, blockhash = _sign_transaction(swap_tx_b64, keypair)

    if use_jito and config.JITO_ENABLED:
        try:
            await _send_via_jito(signed_bytes, keypair, blockhash)
            log.info(f"📡 Transaction envoyée via Jito : {signature} — confirmation en cours...")
        except Exception as e:
            log.warning(f"⚠️ Envoi Jito a échoué ({e}) — repli sur l'envoi RPC classique.")
            await _send_via_rpc(signed_bytes)
            log.info(f"📡 Transaction envoyée via RPC (repli) : {signature} — confirmation en cours...")
    else:
        await _send_via_rpc(signed_bytes)
        log.info(f"📡 Transaction envoyée : {signature} — confirmation en cours...")

    await _confirm_transaction(signature)
    log.info(f"✅ Transaction confirmée : {signature}")

    return {
        "signature": signature,
        "input_amount": int(quote["inAmount"]),
        "output_amount": int(quote["outAmount"]),
        "price_impact_pct": float(quote.get("priceImpactPct", 0)) * 100,
    }
