import pytest

import config
import jupiter_executor


def test_confirmed_token_input_must_match_exact_in_amount():
    jupiter_executor._assert_confirmed_token_input_amount("TokenMint", 2_000_000, 2_000_000)

    with pytest.raises(jupiter_executor.SwapError, match="differs from the exact-in request"):
        jupiter_executor._assert_confirmed_token_input_amount("TokenMint", 2_000_000, 1_999_999)


def test_invalid_confirmed_token_input_fails_closed():
    with pytest.raises(jupiter_executor.SwapError, match="amount is invalid"):
        jupiter_executor._assert_confirmed_token_input_amount("TokenMint", 100, 0)


def test_native_sol_input_delta_is_not_compared_to_requested_amount():
    # Confirmed SOL debit includes the network fee and rent, so it is not an
    # exact-input token amount and must be accounted through the native delta.
    jupiter_executor._assert_confirmed_token_input_amount(config.SOL_MINT, 1_000_000, 1_005_000)
