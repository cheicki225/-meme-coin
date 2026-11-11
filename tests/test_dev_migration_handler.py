import asyncio

import main


class Store:
    def __init__(self):
        self.state = {
            "monitored_dev_wallets": {
                "DevWallet": {"mode": "track_creation", "settings": {}}
            }
        }
        self.saved = False

    def save(self):
        self.saved = True


class Notifier:
    def __init__(self):
        self.messages = []

    async def notify(self, *args, **kwargs):
        self.messages.append((args, kwargs))


def test_migration_candidate_escalates_on_multiple_distinct_token_mints():
    bot = main.SniperBot.__new__(main.SniperBot)
    bot.data_store = Store()
    bot.notifier = Notifier()

    asyncio.run(bot.on_dev_token_transfer("DevWallet", "ChildWallet", "MintA", 10.0, "sig-1"))
    candidate = bot.data_store.state["dev_migration_candidates"]["DevWallet:ChildWallet"]
    assert candidate["status"] == "candidate"
    assert len(bot.notifier.messages) == 1

    asyncio.run(bot.on_dev_token_transfer("DevWallet", "ChildWallet", "MintB", 5.0, "sig-2"))
    assert candidate["status"] == "suspected_migration"
    assert len(candidate["token_mints"]) == 2
    assert len(bot.notifier.messages) == 2
    assert bot.data_store.saved is True


def test_migration_candidate_deduplicates_replayed_signature():
    bot = main.SniperBot.__new__(main.SniperBot)
    bot.data_store = Store()
    bot.notifier = Notifier()

    asyncio.run(bot.on_dev_token_transfer("DevWallet", "ChildWallet", "MintA", 10.0, "same-sig"))
    asyncio.run(bot.on_dev_token_transfer("DevWallet", "ChildWallet", "MintA", 10.0, "same-sig"))
    candidate = bot.data_store.state["dev_migration_candidates"]["DevWallet:ChildWallet"]
    assert candidate["transfer_count"] == 1
    assert candidate["token_mints"] == ["MintA"]
    assert len(bot.notifier.messages) == 1

    # Deux mints différents dans la même transaction sont deux indices,
    # même si la signature on-chain est identique.
    asyncio.run(bot.on_dev_token_transfer("DevWallet", "ChildWallet", "MintB", 5.0, "same-sig"))
    assert candidate["transfer_count"] == 2
    assert candidate["status"] == "suspected_migration"
    assert len(bot.notifier.messages) == 2
