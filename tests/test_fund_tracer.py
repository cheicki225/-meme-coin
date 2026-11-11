import asyncio
from unittest.mock import AsyncMock, patch

import fund_tracer


SOURCE = "SourceWallet111"
RECIPIENT_A = "RecipientWalletAAA"
RECIPIENT_B = "RecipientWalletBBB"


def _transfer(source, destination):
    return {
        "program": "system",
        "parsed": {
            "type": "transfer",
            "info": {
                "source": source,
                "destination": destination,
                "lamports": 1_000_000_000,
            },
        },
    }


def test_extracts_only_outgoing_system_transfers():
    tx = {
        "transaction": {
            "message": {
                "instructions": [
                    _transfer(SOURCE, RECIPIENT_A),
                    _transfer("OtherWallet", "NotOurs"),
                    {
                        "program": "spl-token",
                        "parsed": {
                            "type": "transfer",
                            "info": {"source": SOURCE, "destination": "TokenAccount"},
                        },
                    },
                ]
            }
        },
        "meta": {
            "innerInstructions": [
                {"index": 0, "instructions": [_transfer(SOURCE, RECIPIENT_B)]}
            ]
        },
    }

    assert fund_tracer._extract_outgoing_recipients(tx, SOURCE) == {
        RECIPIENT_A,
        RECIPIENT_B,
    }


def test_ignores_self_transfers_and_malformed_instructions():
    tx = {
        "transaction": {
            "message": {
                "instructions": [
                    _transfer(SOURCE, SOURCE),
                    {"program": "system", "parsed": {"type": "transfer", "info": {}}},
                    None,
                ]
            }
        },
        "meta": {"innerInstructions": [{"instructions": [None]}]},
    }

    assert fund_tracer._extract_outgoing_recipients(tx, SOURCE) == set()


def test_counts_unique_recipients_across_sampled_transactions():
    transactions = {
        "sig-1": {
            "transaction": {"message": {"instructions": [_transfer(SOURCE, RECIPIENT_A)]}},
            "meta": {"innerInstructions": []},
        },
        "sig-2": {
            "transaction": {"message": {"instructions": [_transfer(SOURCE, RECIPIENT_A)]}},
            "meta": {"innerInstructions": [{"instructions": [_transfer(SOURCE, RECIPIENT_B)]}]},
        },
    }

    async def run():
        with patch.object(
            fund_tracer,
            "_get_all_signatures",
            new=AsyncMock(return_value=[{"signature": "sig-1"}, {"signature": "sig-2"}]),
        ), patch.object(
            fund_tracer,
            "_get_transaction",
            new=AsyncMock(side_effect=lambda signature: transactions[signature]),
        ):
            return await fund_tracer._count_distinct_outgoing_recipients(SOURCE, sample_size=10)

    assert asyncio.run(run()) == 2


def test_count_returns_zero_for_invalid_sample_size():
    async def run():
        with patch.object(fund_tracer, "_get_all_signatures", new=AsyncMock()) as get_sigs:
            result = await fund_tracer._count_distinct_outgoing_recipients(SOURCE, sample_size=0)
            get_sigs.assert_not_awaited()
            return result

    assert asyncio.run(run()) == 0
