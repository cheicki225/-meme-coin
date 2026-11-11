import asyncio

from paper_trader import PaperTrader


class Store:
    def __init__(self):
        self.state = {"open_positions": [], "total_pnl_usd": 0.0, "trades": []}
        self.saved = False

    def save(self):
        self.saved = True


def _position():
    return {
        "token_mint": "MintA",
        "source_wallet": "DevWallet",
        "units": 100.0,
        "cost_basis_usd": 50.0,
        "total_sol_received": 0.0,
        "settings": {},
        "tx_signatures": [],
        "initials_sold": False,
    }


def test_sell_initials_uses_execution_and_records_confirmed_proceeds():
    trader = PaperTrader.__new__(PaperTrader)
    trader.data_store = Store()
    trader.notifier = None
    calls = []

    async def execute(token, units, price, settings):
        calls.append((token, units))
        return {"proceeds_usd": 50.0, "sol_received": 0.25, "signature": "sig"}

    trader._execute_sell = execute
    position = _position()
    done = asyncio.run(trader.sell_initials(position, 1.0))

    assert done is True
    assert calls == [("MintA", 50.0)]
    assert position["units"] == 50.0
    assert position["cost_basis_usd"] == 0.0
    assert position["initials_sold"] is True
    assert position["total_sol_received"] == 0.25
    assert "sig" in position["tx_signatures"]
    assert trader.data_store.saved is True


def test_sell_initials_does_not_mutate_position_when_execution_fails():
    trader = PaperTrader.__new__(PaperTrader)
    trader.data_store = Store()
    trader.notifier = None

    async def execute(*args):
        return None

    trader._execute_sell = execute
    position = _position()
    done = asyncio.run(trader.sell_initials(position, 1.0))

    assert done is False
    assert position["units"] == 100.0
    assert position["cost_basis_usd"] == 50.0
    assert position["initials_sold"] is False



def test_copy_sell_uses_configured_percentage_of_remaining_position():
    trader = PaperTrader.__new__(PaperTrader)
    calls = []

    async def partial(position, price, change_pct, ratio, tp_index):
        calls.append(ratio)
        position["units"] *= (1 - ratio)

    async def full(position, price, change_pct, reason):
        calls.append(reason)
        position["units"] = 0

    trader._partial_close = partial
    trader._close_remaining = full
    position = _position()
    assert asyncio.run(trader.copy_sell(position, 1.0, 25.0)) is True
    assert calls == [0.25]
    assert position["units"] == 75.0


def test_copy_sell_rejects_invalid_percentage():
    trader = PaperTrader.__new__(PaperTrader)
    position = _position()

    async def must_not_sell(*args, **kwargs):
        raise AssertionError("invalid copy sell percentage must not execute")

    trader._partial_close = must_not_sell
    trader._close_remaining = must_not_sell
    for value in (0, -1, 101, float("nan"), float("inf")):
        assert asyncio.run(trader.copy_sell(position, 1.0, value)) is False
