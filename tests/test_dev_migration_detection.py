import asyncio

import config
import wallet
from copytrade_listener import CopyTradeListener


class Store:
    def __init__(self):
        self.state = {
            "monitored_dev_wallets": {
                "DevWallet": {"mode": "track_creation", "settings": {}}
            }
        }


def test_dev_token_transfers_emit_candidate_only_and_ignore_swaps(monkeypatch):
    events = []

    async def on_transfer(*args):
        events.append(args)

    listener = CopyTradeListener(
        Store(), on_buy=None, on_sell=None, on_dev_token_transfer=on_transfer
    )
    listener._creation_mode_wallets = {"DevWallet"}
    parsed = {
        "tokenTransfers": [
            {"fromUserAccount": "DevWallet", "toUserAccount": "ChildWallet",
             "mint": "MintA", "tokenAmount": 12.5},
            {"fromUserAccount": "Other", "toUserAccount": "ChildWallet",
             "mint": "MintB", "tokenAmount": 5},
            {"fromUserAccount": "DevWallet", "toUserAccount": "ChildWallet",
             "mint": wallet.USDC_MINT, "tokenAmount": 50},
        ]
    }

    asyncio.run(listener._check_dev_token_transfers(parsed, "sig-1", False))
    assert events == [("DevWallet", "ChildWallet", "MintA", 12.5, "sig-1")]

    asyncio.run(listener._check_dev_token_transfers(parsed, "sig-2", True))
    assert len(events) == 1
