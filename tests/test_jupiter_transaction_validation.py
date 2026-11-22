import pytest
import struct

import config
from solders.hash import Hash
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from jupiter_executor import SwapError, _validate_unsigned_transaction


JUPITER = Pubkey.from_string("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4")
TOKEN = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")


def _transaction(program_id, data=b""):
    payer = Keypair()
    instruction = Instruction(Pubkey.from_string(program_id), data, [])
    message = MessageV0.try_compile(payer.pubkey(), [instruction], [], Hash.default())
    return payer, bytes(VersionedTransaction(message, [payer]))


def _swap_transaction(
    *,
    input_amount=1_000,
    output_amount=900,
    slippage=100,
    discriminator=b"\xe5\x17\xcb\x97\x7a\xe3\xad\x2a",
    duplicate=False,
):
    payer = Keypair()
    input_mint = Pubkey.from_string(config.SOL_MINT)
    output_mint = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
    source_ata, _ = Pubkey.find_program_address(
        [bytes(payer.pubkey()), bytes(TOKEN), bytes(input_mint)], ASSOCIATED
    )
    destination_ata, _ = Pubkey.find_program_address(
        [bytes(payer.pubkey()), bytes(TOKEN), bytes(output_mint)], ASSOCIATED
    )
    route_data = (
        discriminator
        + struct.pack("<I", 1)
        + b"\x00\x00\x00\x00"
        + struct.pack("<QQHB", input_amount, output_amount, slippage, 0)
    )
    instruction = Instruction(
        JUPITER,
        route_data,
        [
            AccountMeta(TOKEN, False, False),
            AccountMeta(payer.pubkey(), True, False),
            AccountMeta(source_ata, False, True),
            AccountMeta(destination_ata, False, True),
            AccountMeta(Pubkey.default(), False, False),
            AccountMeta(output_mint, False, False),
        ],
    )
    instructions = [instruction, instruction] if duplicate else [instruction]
    message = MessageV0.try_compile(payer.pubkey(), instructions, [], Hash.default())
    return payer, bytes(VersionedTransaction(message, [payer])), str(output_mint)


def _shared_accounts_route_transaction(
    *,
    source_mint=None,
    destination_mint=None,
    source_ata=None,
    destination_ata=None,
    input_amount=1_000,
    output_amount=900,
    slippage=100,
    platform_fee_bps=0,
):
    """Build the IDL-ordered sharedAccountsRoute exact-in instruction fixture."""
    payer = Keypair()
    expected_source_mint = Pubkey.from_string(config.SOL_MINT)
    expected_destination_mint = Pubkey.from_string(
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    )
    input_mint = source_mint or expected_source_mint
    output_mint = destination_mint or expected_destination_mint
    expected_source_ata, _ = Pubkey.find_program_address(
        [bytes(payer.pubkey()), bytes(TOKEN), bytes(expected_source_mint)], ASSOCIATED
    )
    expected_destination_ata, _ = Pubkey.find_program_address(
        [bytes(payer.pubkey()), bytes(TOKEN), bytes(expected_destination_mint)], ASSOCIATED
    )
    route_step = bytes([0, 100, 0, 1])  # Saber enum variant; percent, inputIndex, outputIndex.
    route_data = (
        bytes.fromhex("c1209b3341d69c81")
        + struct.pack("<B", 0)  # authority id
        + struct.pack("<I", 1)  # one RoutePlanStep
        + route_step
        + struct.pack("<QQHB", input_amount, output_amount, slippage, platform_fee_bps)
    )
    program_authority, _ = Pubkey.find_program_address(
        [b"authority", bytes([0])], JUPITER
    )
    event_authority, _ = Pubkey.find_program_address([b"__event_authority"], JUPITER)
    instruction = Instruction(
        JUPITER,
        route_data,
        [
            AccountMeta(TOKEN, False, False),  # tokenProgram
            AccountMeta(program_authority, False, False),  # programAuthority
            AccountMeta(payer.pubkey(), True, False),  # userTransferAuthority
            AccountMeta(source_ata or expected_source_ata, False, True),  # sourceTokenAccount
            AccountMeta(Pubkey.new_unique(), False, True),  # programSourceTokenAccount
            AccountMeta(Pubkey.new_unique(), False, True),  # programDestinationTokenAccount
            AccountMeta(destination_ata or expected_destination_ata, False, True),  # destinationTokenAccount
            AccountMeta(input_mint, False, False),  # sourceMint
            AccountMeta(output_mint, False, False),  # destinationMint
            AccountMeta(Pubkey.default(), False, True),  # optional platformFeeAccount
            AccountMeta(Pubkey.default(), False, False),  # optional token2022Program
            AccountMeta(event_authority, False, False),
            AccountMeta(JUPITER, False, False),
        ],
    )
    message = MessageV0.try_compile(payer.pubkey(), [instruction], [], Hash.default())
    return (
        payer,
        bytes(VersionedTransaction(message, [payer])),
        str(expected_destination_mint),
    )


def test_transaction_validator_accepts_only_one_signer_and_jupiter_program():
    payer, transaction, output_mint = _swap_transaction()
    parsed, blockhash = _validate_unsigned_transaction(
        transaction,
        payer,
        config.SOL_MINT,
        1_000,
        output_mint,
        expected_out_amount=900,
        slippage_bps=100,
    )
    assert parsed.message.header.num_required_signatures == 1
    assert blockhash


def test_transaction_validator_accepts_idl_ordered_shared_accounts_route_exact_in():
    payer, transaction, output_mint = _shared_accounts_route_transaction()
    parsed, blockhash = _validate_unsigned_transaction(
        transaction,
        payer,
        config.SOL_MINT,
        1_000,
        output_mint,
        expected_out_amount=900,
        slippage_bps=100,
    )
    assert parsed.message.header.num_required_signatures == 1
    assert blockhash


@pytest.mark.parametrize("instruction_kwargs", [
    {"source_mint": Pubkey.new_unique()},
    {"destination_mint": Pubkey.new_unique()},
    {"source_ata": Pubkey.new_unique()},
    {"destination_ata": Pubkey.new_unique()},
    {"input_amount": 999},
    {"output_amount": 899},
    {"slippage": 99},
    {"platform_fee_bps": 1},
])
def test_transaction_validator_rejects_shared_route_if_mints_accounts_or_terms_mismatch(
    instruction_kwargs,
):
    payer, transaction, output_mint = _shared_accounts_route_transaction(**instruction_kwargs)
    with pytest.raises(SwapError):
        _validate_unsigned_transaction(
            transaction,
            payer,
            config.SOL_MINT,
            1_000,
            output_mint,
            expected_out_amount=900,
            slippage_bps=100,
        )


def test_transaction_validator_rejects_unknown_top_level_program():
    payer, transaction = _transaction("11111111111111111111111111111111")
    with pytest.raises(SwapError, match="transfert SOL non autorisée"):
        _validate_unsigned_transaction(
            transaction,
            payer,
            config.SOL_MINT,
            1_000,
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            expected_out_amount=900,
            slippage_bps=100,
        )


@pytest.mark.parametrize("instruction_kwargs", [
    {"input_amount": 999},
    {"output_amount": 899},
    {"slippage": 99},
    {"discriminator": b"\x00" * 8},
])
def test_transaction_validator_rejects_jupiter_instruction_unbound_to_quote(instruction_kwargs):
    payer, transaction, output_mint = _swap_transaction(**instruction_kwargs)
    with pytest.raises(SwapError):
        _validate_unsigned_transaction(
            transaction,
            payer,
            config.SOL_MINT,
            1_000,
            output_mint,
            expected_out_amount=900,
            slippage_bps=100,
        )


def test_transaction_validator_rejects_empty_jupiter_instruction():
    payer, transaction = _transaction("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4")
    with pytest.raises(SwapError, match="vide ou tronquée"):
        _validate_unsigned_transaction(
            transaction,
            payer,
            config.SOL_MINT,
            1_000,
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            expected_out_amount=900,
            slippage_bps=100,
        )


def test_transaction_validator_rejects_multiple_jupiter_swaps():
    payer, transaction, output_mint = _swap_transaction(duplicate=True)
    with pytest.raises(SwapError, match="Plus d'une"):
        _validate_unsigned_transaction(
            transaction,
            payer,
            config.SOL_MINT,
            1_000,
            output_mint,
            expected_out_amount=900,
            slippage_bps=100,
        )


def test_transaction_validator_rejects_unreadable_transaction_and_wrong_payer():
    payer, transaction, output_mint = _swap_transaction()
    with pytest.raises(SwapError, match="illisible ou invalide"):
        _validate_unsigned_transaction(
            b"not-a-transaction", payer, config.SOL_MINT, 1_000, output_mint,
            expected_out_amount=900, slippage_bps=100,
        )

    with pytest.raises(SwapError, match="payeur"):
        _validate_unsigned_transaction(
            transaction, Keypair(), config.SOL_MINT, 1_000, output_mint,
            expected_out_amount=900, slippage_bps=100,
        )


def test_transaction_validator_requires_lookup_table_addresses_to_be_resolved():
    payer = Keypair()
    loaded_address = Pubkey.new_unique()
    lookup_table = AddressLookupTableAccount(Pubkey.new_unique(), [loaded_address])
    instruction = Instruction(
        Pubkey.from_string("ComputeBudget111111111111111111111111111111"),
        b"\x02\x00\x00\x00",
        [AccountMeta(loaded_address, False, False)],
    )
    message = MessageV0.try_compile(payer.pubkey(), [instruction], [lookup_table], Hash.default())
    transaction = bytes(VersionedTransaction(message, [payer]))
    assert message.address_table_lookups

    with pytest.raises(SwapError, match="must be resolved"):
        _validate_unsigned_transaction(
            transaction, payer, config.SOL_MINT, 1_000,
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            expected_out_amount=900, slippage_bps=100,
        )
    with pytest.raises(SwapError, match="resolution is incomplete"):
        _validate_unsigned_transaction(
            transaction, payer, config.SOL_MINT, 1_000,
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            expected_out_amount=900, slippage_bps=100,
            resolved_lookup_addresses=([], []),
        )


def test_transaction_validator_rejects_additional_signers_and_missing_quote_binding():
    payer, second_signer = Keypair(), Keypair()
    multi_signer_instruction = Instruction(
        Pubkey.new_unique(),
        b"fixture",
        [
            AccountMeta(payer.pubkey(), True, False),
            AccountMeta(second_signer.pubkey(), True, False),
        ],
    )
    multi_signer_message = MessageV0.try_compile(
        payer.pubkey(), [multi_signer_instruction], [], Hash.default(),
    )
    multi_signer_transaction = bytes(
        VersionedTransaction(multi_signer_message, [payer, second_signer])
    )
    with pytest.raises(SwapError, match="signataires supplémentaires"):
        _validate_unsigned_transaction(
            multi_signer_transaction, payer, config.SOL_MINT, 1_000,
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            expected_out_amount=900, slippage_bps=100,
        )

    payer, transaction, output_mint = _swap_transaction()
    with pytest.raises(SwapError, match="attentes du devis manquent"):
        _validate_unsigned_transaction(
            transaction, payer, config.SOL_MINT, 1_000, output_mint,
            expected_out_amount=None, slippage_bps=100,
        )


def test_transaction_validator_rejects_non_swap_top_level_transaction():
    payer, transaction = _transaction("ComputeBudget111111111111111111111111111111", b"\x02\x00\x00\x00")
    with pytest.raises(SwapError, match="ne contient pas d'instruction de swap"):
        _validate_unsigned_transaction(
            transaction, payer, config.SOL_MINT, 1_000,
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            expected_out_amount=900, slippage_bps=100,
        )

