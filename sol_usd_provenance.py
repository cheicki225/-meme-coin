"""Parse timestamped SOL/USD observations without promoting caller data to trust.

This module deliberately does not implement a network client or make a quote
eligible for financial accounting. It provides a strict, offline-testable
contract for a future Pyth Hermes transport. Parsed provider JSON is still
provider-reported data until its signed update is verified independently.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import math
from typing import Callable, Mapping, Protocol


class SolUsdQuoteUnavailable(RuntimeError):
    """Raised when an observation is malformed, stale, or cannot be trusted."""


class LatestPriceTransport(Protocol):
    """Read-only transport interface; tests provide an offline implementation."""

    provider_source: str

    async def fetch_latest(self, feed_id: str) -> Mapping[str, object]:
        """Return one provider response for the requested fixed feed ID."""


@dataclass(frozen=True)
class SolUsdQuoteObservation:
    """An immutable provider-reported observation with explicit trust limits."""

    provider_id: str
    provider_source: str
    provider_source_status: str
    pair: str
    feed_id: str
    price_usd: Decimal
    confidence_usd: Decimal
    confidence_ratio: Decimal
    provider_publish_time_utc: datetime
    received_at_utc: datetime
    age_seconds: float
    integrity_status: str
    feed_mapping_status: str
    trust_status: str
    signed_payload_present: bool
    signature_verified: bool
    signature_status: str
    independent_check_status: str
    quote_status: str

    @property
    def accounting_eligible(self) -> bool:
        """No observation from this parser is eligible for financial accounting."""
        return False


class PythHermesQuoteAdapter:
    """Validate Pyth Hermes fields while keeping provider trust unverified.

    ``expected_feed_id`` and ``max_age_seconds`` must come from reviewed local
    configuration. This adapter does not authenticate an HTTP origin, verify a
    Pyth signature, choose an API key, or contact a provider by itself.
    """

    provider_id = "pyth_hermes"
    pair = "SOL/USD"

    def __init__(
        self,
        expected_feed_id: str,
        *,
        max_age_seconds: float,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(expected_feed_id, str) or len(expected_feed_id) != 64:
            raise ValueError("expected_feed_id must be a 32-byte hexadecimal ID")
        if any(character not in "0123456789abcdefABCDEF" for character in expected_feed_id):
            raise ValueError("expected_feed_id must be hexadecimal")
        if (
            isinstance(max_age_seconds, bool)
            or not isinstance(max_age_seconds, (int, float))
            or not math.isfinite(max_age_seconds)
            or max_age_seconds <= 0
        ):
            raise ValueError("max_age_seconds must be positive")
        self._expected_feed_id = expected_feed_id.lower()
        self._max_age_seconds = float(max_age_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def observe(self, transport: LatestPriceTransport) -> SolUsdQuoteObservation:
        """Fetch one observation through an injected transport, without trust promotion."""
        provider_source = getattr(transport, "provider_source", None)
        if not isinstance(provider_source, str) or not provider_source.strip():
            raise SolUsdQuoteUnavailable("provider_source_missing")
        if provider_source.strip().lower() != self.provider_id:
            raise SolUsdQuoteUnavailable("provider_source_unrecognized")
        try:
            response = await transport.fetch_latest(self._expected_feed_id)
        except Exception:
            raise SolUsdQuoteUnavailable("provider_transport_unavailable") from None
        return self.parse_response(
            response,
            received_at_utc=self._clock(),
            provider_source=provider_source,
        )

    def parse_response(
        self,
        response: Mapping[str, object],
        *,
        received_at_utc: datetime,
        provider_source: str | None = None,
    ) -> SolUsdQuoteObservation:
        """Parse one Hermes-shaped response and enforce feed, age, and numeric checks."""
        if not isinstance(provider_source, str) or not provider_source.strip():
            raise SolUsdQuoteUnavailable("provider_source_missing")
        normalized_source = provider_source.strip().lower()
        if normalized_source != self.provider_id:
            raise SolUsdQuoteUnavailable("provider_source_unrecognized")
        if not isinstance(response, Mapping):
            raise SolUsdQuoteUnavailable("invalid_provider_response")
        parsed = response.get("parsed")
        if not isinstance(parsed, list) or len(parsed) != 1:
            raise SolUsdQuoteUnavailable("invalid_provider_response")
        update = parsed[0]
        if not isinstance(update, Mapping):
            raise SolUsdQuoteUnavailable("invalid_provider_response")
        feed_id = update.get("id")
        if not isinstance(feed_id, str) or feed_id.lower() != self._expected_feed_id:
            raise SolUsdQuoteUnavailable("unexpected_price_feed")
        price_record = update.get("price")
        if not isinstance(price_record, Mapping):
            raise SolUsdQuoteUnavailable("invalid_provider_response")

        price_raw = price_record.get("price")
        confidence_raw = price_record.get("conf")
        exponent = price_record.get("expo")
        publish_time = price_record.get("publish_time")
        if (
            not isinstance(price_raw, str)
            or not price_raw.lstrip("-").isdigit()
            or not isinstance(confidence_raw, str)
            or not confidence_raw.isdigit()
            or type(exponent) is not int
            or not -38 <= exponent <= 38
            or type(publish_time) is not int
            or publish_time < 0
        ):
            raise SolUsdQuoteUnavailable("invalid_provider_numeric_fields")

        binary = response.get("binary")
        signed_payload_present = False
        if isinstance(binary, Mapping):
            encoding = binary.get("encoding")
            data = binary.get("data")
            signed_payload_present = (
                encoding in {"hex", "base64"}
                and isinstance(data, list)
                and len(data) == 1
                and isinstance(data[0], str)
                and bool(data[0])
            )
        if not signed_payload_present:
            raise SolUsdQuoteUnavailable("signed_update_payload_missing")

        try:
            price = Decimal(price_raw).scaleb(exponent)
            confidence = Decimal(confidence_raw).scaleb(exponent)
            provider_timestamp = datetime.fromtimestamp(publish_time, timezone.utc)
        except (InvalidOperation, OverflowError, OSError, ValueError):
            raise SolUsdQuoteUnavailable("invalid_provider_numeric_fields") from None
        if not price.is_finite() or price <= 0 or not confidence.is_finite() or confidence < 0:
            raise SolUsdQuoteUnavailable("invalid_provider_numeric_fields")
        # A confidence interval at least as wide as the quoted midpoint has no
        # strictly positive lower bound, so it cannot represent a coherent
        # positive SOL/USD observation for valuation.
        if confidence >= price:
            raise SolUsdQuoteUnavailable("incoherent_price_confidence")
        if not isinstance(received_at_utc, datetime) or received_at_utc.tzinfo is None:
            raise SolUsdQuoteUnavailable("invalid_local_receive_time")
        received_at_utc = received_at_utc.astimezone(timezone.utc)
        age_seconds = (received_at_utc - provider_timestamp).total_seconds()
        if age_seconds < 0:
            raise SolUsdQuoteUnavailable("provider_timestamp_in_future")
        if age_seconds > self._max_age_seconds:
            raise SolUsdQuoteUnavailable("provider_quote_stale")

        return SolUsdQuoteObservation(
            provider_id=self.provider_id,
            provider_source=normalized_source,
            provider_source_status="transport_asserted_unverified",
            pair=self.pair,
            feed_id=self._expected_feed_id,
            price_usd=price,
            confidence_usd=confidence,
            confidence_ratio=confidence / price,
            provider_publish_time_utc=provider_timestamp,
            received_at_utc=received_at_utc,
            age_seconds=age_seconds,
            integrity_status="structure_numeric_and_age_checks_passed",
            feed_mapping_status="caller_configured_unverified",
            trust_status="unknown",
            signed_payload_present=True,
            signature_verified=False,
            signature_status="payload_present_unverified",
            independent_check_status="unavailable",
            quote_status="unknown",
        )
