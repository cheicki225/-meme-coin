import pytest
import struct

import config
import jupiter_executor
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
@pytest.mark.parametrize("value", [None, True, 1.5, "", "-1"])
def test_jupiter_critical_numeric_helpers_fail_closed(value, monkeypatch):
    with pytest.raises(SwapError):
        jupiter_executor._strict_uint(value, "amount")

    monkeypatch.setattr(config, "MAX_SLIPPAGE_BPS", value, raising=False)
    with pytest.raises(SwapError, match="MAX_SLIPPAGE_BPS"):
        jupiter_executor._maximum_allowed_slippage_bps()


def test_jupiter_slippage_cap_and_price_impact_configuration_are_bounded(monkeypatch):
    monkeypatch.setattr(config, "MAX_SLIPPAGE_BPS", 2_000, raising=False)
    assert jupiter_executor._maximum_allowed_slippage_bps() == 1_000

    monkeypatch.setattr(config, "MAX_PRICE_IMPACT_PCT", 9.0, raising=False)
    assert jupiter_executor._maximum_allowed_price_impact_pct() == 5.0
    monkeypatch.setattr(config, "MAX_PRICE_IMPACT_PCT", 2.25, raising=False)
    assert jupiter_executor._maximum_allowed_price_impact_pct() == 2.25


@pytest.mark.parametrize("value", [None, True, "1", float("nan"), float("inf"), -0.01])
def test_jupiter_price_impact_limit_rejects_invalid_configuration(monkeypatch, value):
    monkeypatch.setattr(config, "MAX_PRICE_IMPACT_PCT", value, raising=False)
    with pytest.raises(SwapError, match="MAX_PRICE_IMPACT_PCT"):
        jupiter_executor._maximum_allowed_price_impact_pct()


@pytest.mark.parametrize(
    "quote",
    [
        {},
        {"priceImpact": True},
        {"priceImpact": "invalid"},
        {"priceImpactPct": "nan"},
        {"priceImpactPct": "inf"},
        {"priceImpact": 1.0, "priceImpactPct": 0.02},
    ],
)
def test_jupiter_price_impact_quote_rejects_missing_or_bad_fields(quote):
    with pytest.raises(SwapError):
        jupiter_executor._quote_price_impact_percent(quote)


@pytest.mark.parametrize(
    "quote,expected",
    [
        ({"priceImpact": -1.25}, 1.25),
        ({"priceImpactPct": "0.0125"}, 1.25),
        ({"priceImpact": 1.25, "priceImpactPct": "0.0125"}, 1.25),
    ],
)
def test_jupiter_price_impact_quote_parses_supported_units(quote, expected):
    assert jupiter_executor._quote_price_impact_percent(quote) == expected


def _direct_jupiter_swap_fixture():
    payer, raw, output_mint = _swap_transaction()
    transaction = VersionedTransaction.from_bytes(raw)
    instruction = transaction.message.instructions[0]
    account_keys = list(transaction.message.account_keys)
    accounts = [account_keys[int(index)] for index in instruction.accounts]
    return payer, bytes(instruction.data), accounts, Pubkey.from_string(output_mint)


@pytest.mark.parametrize(
    "mutation,error",
    [
        ("truncated", "tronquée"),
        ("unsupported", "non prise en charge"),
        ("missing_accounts", "manquants"),
        ("wrong_authority", "autorité de transfert"),
        ("wrong_output_mint", "mint de sortie"),
        ("wrong_token_program", "Programme SPL"),
        ("wrong_source_ata", "compte source"),
        ("wrong_destination_ata", "compte destination"),
        ("empty_route", "Route plan"),
        ("oversized_route", "Route plan"),
        ("wrong_input_amount", "montants"),
        ("wrong_output_amount", "montants"),
        ("wrong_slippage", "slippage"),
        ("platform_fee", "frais de plateforme"),
    ],
)
def test_jupiter_swap_instruction_rejects_financial_mismatches(mutation, error):
    payer, data, accounts, output_mint = _direct_jupiter_swap_fixture()
    if mutation == "truncated":
        data = b"short"
    elif mutation == "unsupported":
        data = bytes(8) + data[8:]
    elif mutation == "missing_accounts":
        accounts = []
    elif mutation == "wrong_authority":
        accounts[1] = Pubkey.new_unique()
    elif mutation == "wrong_output_mint":
        accounts[5] = Pubkey.new_unique()
    elif mutation == "wrong_token_program":
        accounts[0] = Pubkey.new_unique()
    elif mutation == "wrong_source_ata":
        accounts[2] = Pubkey.new_unique()
    elif mutation == "wrong_destination_ata":
        accounts[3] = Pubkey.new_unique()
    elif mutation == "empty_route":
        data = data[:8] + (0).to_bytes(4, "little") + data[12:]
    elif mutation == "oversized_route":
        data = data[:8] + (65).to_bytes(4, "little") + data[12:]
    elif mutation == "wrong_input_amount":
        data = data[:-19] + (999).to_bytes(8, "little") + data[-11:]
    elif mutation == "wrong_output_amount":
        data = data[:-11] + (899).to_bytes(8, "little") + data[-3:]
    elif mutation == "wrong_slippage":
        data = data[:-3] + (101).to_bytes(2, "little") + data[-1:]
    elif mutation == "platform_fee":
        data = data[:-1] + b"\x01"

    with pytest.raises(SwapError, match=error):
        jupiter_executor._validate_jupiter_swap_instruction(
            data,
            accounts,
            owner=payer.pubkey(),
            input_mint=Pubkey.from_string(config.SOL_MINT),
            output_mint=output_mint,
            expected_in_amount=1_000,
            expected_out_amount=900,
            slippage_bps=100,
        )
def test_shared_accounts_route_accepts_explicit_token_2022_destination_program():
    payer, raw, output_mint = _shared_accounts_route_transaction()
    transaction = VersionedTransaction.from_bytes(raw)
    instruction = transaction.message.instructions[0]
    account_keys = list(transaction.message.account_keys)
    accounts = [account_keys[int(index)] for index in instruction.accounts]
    token_2022 = Pubkey.from_string(
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
    )
    accounts[10] = token_2022
    output_mint_key = Pubkey.from_string(output_mint)
    accounts[6], _ = Pubkey.find_program_address(
        [bytes(payer.pubkey()), bytes(token_2022), bytes(output_mint_key)], ASSOCIATED
    )

    jupiter_executor._validate_jupiter_swap_instruction(
        bytes(instruction.data),
        accounts,
        owner=payer.pubkey(),
        input_mint=Pubkey.from_string(config.SOL_MINT),
        output_mint=output_mint_key,
        expected_in_amount=1_000,
        expected_out_amount=900,
        slippage_bps=100,
    )


def test_shared_accounts_route_rejects_truncated_route_plan_after_account_validation():
    payer, raw, output_mint = _shared_accounts_route_transaction()
    transaction = VersionedTransaction.from_bytes(raw)
    instruction = transaction.message.instructions[0]
    account_keys = list(transaction.message.account_keys)
    accounts = [account_keys[int(index)] for index in instruction.accounts]

    with pytest.raises(SwapError, match="Route plan Jupiter incomplet"):
        jupiter_executor._validate_jupiter_swap_instruction(
            bytes(instruction.data)[:-1],
            accounts,
            owner=payer.pubkey(),
            input_mint=Pubkey.from_string(config.SOL_MINT),
            output_mint=Pubkey.from_string(output_mint),
            expected_in_amount=1_000,
            expected_out_amount=900,
            slippage_bps=100,
        )
