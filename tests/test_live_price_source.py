import asyncio

import pytest


@pytest.mark.parametrize("operation", ["buy", "sell"])
def test_live_swap_is_not_submitted_when_sol_usd_quote_is_unavailable(monkeypatch, operation):
    from backtest import SolUsdPriceUnavailable
    import live_trader
    from live_trader import LiveTrader

    class Store:
        state = {
            "emergency_stop": False,
            "pending_transactions": [],
            "open_positions": [
                {
                    "token_mint": "TokenMint",
                    "token_decimals": 6,
                    "execution_wallet": "WalletPubkey",
                }
            ],
        }

    async def unavailable_quote():
        raise SolUsdPriceUnavailable("offline")

    monkeypatch.setattr(live_trader, "get_sol_usd_rate", unavailable_quote)
    trader = LiveTrader(Store())
    swaps = []

    async def unexpected_swap(*_args, **_kwargs):
        swaps.append(True)
        raise AssertionError("No swap may be submitted without a SOL/USD quote")

    trader._execute_swap_tracked = unexpected_swap
    trader._get_execution_keypair = lambda: pytest.fail("Wallet access must follow price validation")
    trader._get_position_keypair = lambda *_args: pytest.fail("Wallet access must follow price validation")

    with pytest.raises(SolUsdPriceUnavailable, match="offline"):
        if operation == "buy":
            asyncio.run(trader._execute_buy("TokenMint", 1.0, {"buy_amount_sol": 0.1}))
        else:
            asyncio.run(trader._execute_sell("TokenMint", 1.0, 1.0, {}))

    assert swaps == []
