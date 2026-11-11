import pytest

from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from jupiter_executor import SwapError, _validate_unsigned_transaction


def _transaction(program_id):
    payer = Keypair()
    instruction = Instruction(Pubkey.from_string(program_id), b"", [])
    message = MessageV0.try_compile(payer.pubkey(), [instruction], [], Hash.default())
    return payer, bytes(VersionedTransaction(message, [payer]))


def test_transaction_validator_accepts_only_one_signer_and_jupiter_program():
    payer, transaction = _transaction("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4")
    parsed, blockhash = _validate_unsigned_transaction(
        transaction, payer, "So11111111111111111111111111111111111111112", 1_000
    )
    assert parsed.message.header.num_required_signatures == 1
    assert blockhash


def test_transaction_validator_rejects_unknown_top_level_program():
    payer, transaction = _transaction("11111111111111111111111111111111")
    with pytest.raises(SwapError, match="transfert SOL non autorisée"):
        _validate_unsigned_transaction(
            transaction, payer, "So11111111111111111111111111111111111111112", 1_000
        )

