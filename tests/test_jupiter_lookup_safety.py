import asyncio

import pytest

import config
import jupiter_executor
import rpc_client
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction


JUPITER = Pubkey.from_string("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4")
ASSOCIATED = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
TOKEN = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
SYSTEM = Pubkey.from_string("11111111111111111111111111111111")


def _lookup_transaction():
    payer = Keypair()
    loaded = Pubkey.new_unique()
    lookup_table = AddressLookupTableAccount(Pubkey.new_unique(), [loaded])
    instruction = Instruction(JUPITER, b"", [AccountMeta(loaded, False, True)])
    message = MessageV0.try_compile(payer.pubkey(), [instruction], [lookup_table], Hash.default())
    return payer, message, bytes(VersionedTransaction(message, [payer]))


def test_transaction_validator_refuses_unresolved_lookup_indexes():
    payer, _, raw = _lookup_transaction()
    with pytest.raises(jupiter_executor.SwapError, match="must be resolved"):
        jupiter_executor._validate_unsigned_transaction(
            raw, payer, config.SOL_MINT, 1000, "Mint111111111111111111111111111111111111"
        )


def test_lookup_table_resolution_rejects_untrusted_account_owner(monkeypatch):
    _, message, _ = _lookup_transaction()

    async def fake_rpc(*args, **kwargs):
        return {"value": {"owner": str(SYSTEM), "data": ["", "base64"]}}

    monkeypatch.setattr(rpc_client, "rpc_post", fake_rpc)
    with pytest.raises(jupiter_executor.SwapError, match="unexpected program"):
        asyncio.run(jupiter_executor._resolve_lookup_addresses(message))


def _swap_with_ata(payer, owner, mint, ata):
    create_ata = Instruction(
        ASSOCIATED,
        bytes([1]),
        [
            AccountMeta(payer.pubkey(), True, True),
            AccountMeta(ata, False, True),
            AccountMeta(owner, False, False),
            AccountMeta(mint, False, False),
            AccountMeta(SYSTEM, False, False),
            AccountMeta(TOKEN, False, False),
        ],
    )
    message = MessageV0.try_compile(
        payer.pubkey(),
        [Instruction(JUPITER, b"", []), create_ata],
        [],
        Hash.default(),
    )
    return bytes(VersionedTransaction(message, [payer]))


def test_ata_instruction_is_limited_to_the_taker_and_swap_mint():
    payer = Keypair()
    mint = Pubkey.new_unique()
    ata, _ = Pubkey.find_program_address(
        [bytes(payer.pubkey()), bytes(TOKEN), bytes(mint)], ASSOCIATED
    )
    raw = _swap_with_ata(payer, payer.pubkey(), mint, ata)
    jupiter_executor._validate_unsigned_transaction(
        raw, payer, config.SOL_MINT, 1000, str(mint), resolved_lookup_addresses=([], [])
    )


def test_ata_instruction_for_third_party_is_rejected():
    payer = Keypair()
    other = Pubkey.new_unique()
    mint = Pubkey.new_unique()
    ata, _ = Pubkey.find_program_address([bytes(other), bytes(TOKEN), bytes(mint)], ASSOCIATED)
    raw = _swap_with_ata(payer, other, mint, ata)
    with pytest.raises(jupiter_executor.SwapError, match="autre wallet"):
        jupiter_executor._validate_unsigned_transaction(
            raw, payer, config.SOL_MINT, 1000, str(mint), resolved_lookup_addresses=([], [])
        )
