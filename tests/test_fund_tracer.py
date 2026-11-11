import asyncio

import fund_tracer


WALLET = "Wallet111111111111111111111111111111111111"
RECIPIENT_A = "RecipientA11111111111111111111111111111111"
RECIPIENT_B = "RecipientB11111111111111111111111111111111"


def _transaction(destination, *, source=WALLET, error=None, program="system"):
    return {
        "meta": {"err": error},
        "transaction": {
            "message": {
                "instructions": [{
                    "program": program,
                    "programId": "11111111111111111111111111111111",
                    "parsed": {
                        "type": "transfer",
                        "info": {"source": source, "destination": destination, "lamports": 1000},
                    },
                }]
            }
        },
    }


def test_counts_unique_successful_direct_recipients(monkeypatch):
    signatures = [{"signature": f"sig-{i}", "err": None} for i in range(4)]

    async def get_signatures(*args, **kwargs):
        return signatures

    async def get_transaction(signature):
        destination = {
            "sig-0": RECIPIENT_A,
            "sig-1": RECIPIENT_A,
            "sig-2": RECIPIENT_B,
            "sig-3": RECIPIENT_B,
        }[signature]
        error = {"InstructionError": [0, "failed"]} if signature == "sig-3" else None
        return _transaction(destination, error=error)

    monkeypatch.setattr(fund_tracer, "_get_all_signatures", get_signatures)
    monkeypatch.setattr(fund_tracer, "_get_transaction", get_transaction)
    assert asyncio.run(fund_tracer._count_distinct_outgoing_recipients(WALLET)) == 2


def test_ignores_cpi_unrelated_and_malformed_transfers(monkeypatch):
    async def get_signatures(*args, **kwargs):
        return [{"signature": "sig-1"}, {"signature": "sig-2"}]

    async def get_transaction(signature):
        if signature == "sig-1":
            return _transaction(RECIPIENT_A, source="Other111111111111111111111111111111111111")
        tx = _transaction(RECIPIENT_B)
        tx["transaction"]["message"]["instructions"][0]["program"] = "spl-token"
        return tx

    monkeypatch.setattr(fund_tracer, "_get_all_signatures", get_signatures)
    monkeypatch.setattr(fund_tracer, "_get_transaction", get_transaction)
    assert asyncio.run(fund_tracer._count_distinct_outgoing_recipients(WALLET)) == 0
