import pytest

from transaction_accounting import (
    AccountingError,
    account_confirmed_sol_transfer,
    account_confirmed_swap,
)

SOL = "So11111111111111111111111111111111111111112"
TOKEN = "Token1111111111111111111111111111111111111"
WALLET = "Wallet111111111111111111111111111111111111"


def _transaction(*, native_delta=-1_010_000_000, token_delta=2_000_000, fee=10_000_000, extra=None):
    pre = [{"accountIndex": 1, "mint": TOKEN, "owner": WALLET,
            "uiTokenAmount": {"amount": "0", "decimals": 6}}]
    post = [{"accountIndex": 1, "mint": TOKEN, "owner": WALLET,
             "uiTokenAmount": {"amount": str(token_delta), "decimals": 6}}]
    if extra:
        post.append({"accountIndex": 2, "mint": extra, "owner": WALLET,
                     "uiTokenAmount": {"amount": "1", "decimals": 6}})
    return {
        "transaction": {"message": {"accountKeys": [WALLET, "TokenAccount"]}},
        "meta": {"err": None, "fee": fee,
                 "preBalances": [2_000_000_000, 0],
                 "postBalances": [2_000_000_000 + native_delta, 0],
                 "preTokenBalances": pre, "postTokenBalances": post},
    }


def test_buy_uses_confirmed_token_delta_and_fee_inclusive_sol_cashflow():
    result = account_confirmed_swap(WALLET, SOL, TOKEN, _transaction())
    assert result["native_delta_lamports"] == -1_010_000_000
    assert result["fee_lamports"] == 10_000_000
    assert result["output_delta_raw"] == 2_000_000
    assert result["token_decimals"][TOKEN] == 6


def test_sell_uses_actual_token_delta_and_net_sol_cashflow():
    tx = _transaction(native_delta=1_190_000_000, token_delta=0)
    tx["meta"]["preTokenBalances"][0]["uiTokenAmount"]["amount"] = "2_000_000"
    tx["meta"]["postTokenBalances"] = []
    result = account_confirmed_swap(WALLET, TOKEN, SOL, tx)
    assert result["native_delta_lamports"] == 1_190_000_000
    assert result["input_delta_raw"] == -2_000_000


def test_rejects_unrelated_wallet_token_movement():
    with pytest.raises(AccountingError, match="unrelated"):
        account_confirmed_swap(WALLET, SOL, TOKEN, _transaction(extra="OtherMint"))


def test_rejects_unconfirmed_or_failed_transaction_metadata():
    tx = _transaction()
    tx["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    with pytest.raises(AccountingError, match="failed"):
        account_confirmed_swap(WALLET, SOL, TOKEN, tx)


def test_verifies_manual_sol_transfer_destination_amount_and_fee():
    destination = "Destination111111111111111111111111111111"
    transaction = {
        "transaction": {"message": {"accountKeys": [WALLET, destination]}},
        "meta": {"err": None, "fee": 5_000, "preBalances": [2_000_000, 0],
                 "postBalances": [995_000, 1_000_000]},
    }
    result = account_confirmed_sol_transfer(WALLET, destination, 1_000_000, transaction)
    assert result["fee_lamports"] == 5_000
    assert result["destination_delta_lamports"] == 1_000_000

    transaction["meta"]["postBalances"][1] = 999_999
    with pytest.raises(AccountingError, match="deltas"):
        account_confirmed_sol_transfer(WALLET, destination, 1_000_000, transaction)

