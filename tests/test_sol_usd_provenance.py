import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from sol_usd_provenance import PythHermesQuoteAdapter, SolUsdQuoteUnavailable


FEED_ID = "a" * 64
NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)


def _response(*, feed_id=FEED_ID, publish_time=1795607940, price="15000000000"):
    return {
        "binary": {"encoding": "hex", "data": ["signed-update-fixture"]},
        "parsed": [
            {
                "id": feed_id,
                "price": {
                    "price": price,
                    "conf": "12000000",
                    "expo": -8,
                    "publish_time": publish_time,
                },
            }
        ],
    }


class SimulatedHermesTransport:
    provider_source = "pyth_hermes"

    def __init__(self, response):
        self.response = response
        self.requested_feed_ids = []

    async def fetch_latest(self, feed_id):
        self.requested_feed_ids.append(feed_id)
        return self.response


def test_simulated_transport_reports_fields_but_never_authenticates_quote():
    adapter = PythHermesQuoteAdapter(
        FEED_ID, max_age_seconds=120, clock=lambda: NOW
    )
    transport = SimulatedHermesTransport(_response())

    observation = asyncio.run(adapter.observe(transport))

    assert transport.requested_feed_ids == [FEED_ID]
    assert observation.provider_id == "pyth_hermes"
    assert observation.provider_source == "pyth_hermes"
    assert observation.provider_source_status == "transport_asserted_unverified"
    assert observation.pair == "SOL/USD"
    assert observation.feed_id == FEED_ID
    assert observation.price_usd == Decimal("150")
    assert observation.confidence_usd == Decimal("0.12")
    assert observation.confidence_ratio == Decimal("0.0008")
    assert observation.age_seconds == 60
    assert observation.integrity_status == "structure_numeric_and_age_checks_passed"
    assert observation.feed_mapping_status == "caller_configured_unverified"
    assert observation.trust_status == "unknown"
    assert observation.signed_payload_present is True
    assert observation.signature_verified is False
    assert observation.signature_status == "payload_present_unverified"
    assert observation.independent_check_status == "unavailable"
    assert observation.quote_status == "unknown"
    assert observation.accounting_eligible is False


@pytest.mark.parametrize(
    ("response", "max_age", "expected_reason", "source", "confidence"),
    [
        (_response(publish_time=int(NOW.timestamp()) - 121), 120, "provider_quote_stale", "pyth_hermes", None),
        (_response(publish_time=int(NOW.timestamp()) + 1), 120, "provider_timestamp_in_future", "pyth_hermes", None),
        (_response(feed_id="b" * 64), 120, "unexpected_price_feed", "pyth_hermes", None),
        (_response(price=None), 120, "invalid_provider_numeric_fields", "pyth_hermes", None),
        (_response(), 120, "incoherent_price_confidence", "pyth_hermes", "15000000000"),
        (_response(), 120, "provider_source_unrecognized", "unknown_provider", None),
    ],
)
def test_quote_is_refused_when_source_or_price_evidence_is_invalid(
    response, max_age, expected_reason, source, confidence,
):
    adapter = PythHermesQuoteAdapter(FEED_ID, max_age_seconds=max_age, clock=lambda: NOW)
    if confidence is not None:
        response["parsed"][0]["price"]["conf"] = confidence

    with pytest.raises(SolUsdQuoteUnavailable, match=expected_reason):
        adapter.parse_response(response, received_at_utc=NOW, provider_source=source)


def test_quote_without_provider_source_is_refused():
    adapter = PythHermesQuoteAdapter(FEED_ID, max_age_seconds=120, clock=lambda: NOW)
    with pytest.raises(SolUsdQuoteUnavailable, match="provider_source_missing"):
        adapter.parse_response(_response(), received_at_utc=NOW)

    class UnlabelledTransport:
        async def fetch_latest(self, _feed_id):
            raise AssertionError("transport must not be called without a source label")

    with pytest.raises(SolUsdQuoteUnavailable, match="provider_source_missing"):
        asyncio.run(adapter.observe(UnlabelledTransport()))


def test_missing_signed_payload_is_not_treated_as_integrity_evidence():
    response = _response()
    response.pop("binary")
    adapter = PythHermesQuoteAdapter(FEED_ID, max_age_seconds=120, clock=lambda: NOW)

    with pytest.raises(SolUsdQuoteUnavailable, match="signed_update_payload_missing"):
        adapter.parse_response(response, received_at_utc=NOW, provider_source="pyth_hermes")


@pytest.mark.parametrize(
    "field,value",
    [
        ("price", 150),
        ("price", "NaN"),
        ("conf", -1),
        ("expo", True),
        ("expo", 39),
        ("publish_time", True),
    ],
)
def test_malformed_numeric_fields_are_refused(field, value):
    response = _response()
    response["parsed"][0]["price"][field] = value
    adapter = PythHermesQuoteAdapter(FEED_ID, max_age_seconds=120, clock=lambda: NOW)

    with pytest.raises(SolUsdQuoteUnavailable):
        adapter.parse_response(response, received_at_utc=NOW, provider_source="pyth_hermes")


def test_adapter_rejects_invalid_policy_and_naive_clock_values():
    with pytest.raises(ValueError, match="max_age_seconds"):
        PythHermesQuoteAdapter(FEED_ID, max_age_seconds=float("nan"))
    with pytest.raises(ValueError, match="expected_feed_id"):
        PythHermesQuoteAdapter("not-a-feed", max_age_seconds=120)

    adapter = PythHermesQuoteAdapter(FEED_ID, max_age_seconds=120)
    with pytest.raises(SolUsdQuoteUnavailable, match="invalid_local_receive_time"):
        adapter.parse_response(
            _response(), received_at_utc=datetime(2026, 11, 25, 12, 0, 0),
            provider_source="pyth_hermes",
        )


def test_transport_error_is_redacted_from_the_adapter_exception():
    class FailingTransport:
        provider_source = "pyth_hermes"

        async def fetch_latest(self, _feed_id):
            raise RuntimeError("private transport detail")

    adapter = PythHermesQuoteAdapter(
        FEED_ID, max_age_seconds=120, clock=lambda: NOW
    )

    with pytest.raises(SolUsdQuoteUnavailable, match="provider_transport_unavailable") as error:
        asyncio.run(adapter.observe(FailingTransport()))
    assert "private transport detail" not in str(error.value)
