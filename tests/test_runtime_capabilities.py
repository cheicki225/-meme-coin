import pytest

import config
from runtime_capabilities import assess_provider_startup


def test_trading_mode_is_normalized_and_rejects_unknown_values():
    assert config._normalize_trading_mode(" paper ") == "PAPER"
    assert config._normalize_trading_mode("live") == "LIVE"
    with pytest.raises(ValueError):
        config._normalize_trading_mode("simulation")


def test_paper_can_start_with_public_http_but_reports_missing_realtime_features():
    status = assess_provider_startup("PAPER", "")
    assert status["can_start"] is True
    assert status["status"] == "degraded_public_http"
    assert "public endpoint" in status["message"]
    assert "WebSocket" in status["message"]


def test_live_start_is_blocked_without_helius_websocket_credentials():
    status = assess_provider_startup("live", "")
    assert status["can_start"] is False
    assert status["status"] == "blocked_live"


def test_configured_key_is_not_misreported_as_verified_provider_health():
    status = assess_provider_startup("PAPER", "configured-value")
    assert status["status"] == "credentials_configured"
    assert "not health-verified" in status["message"]
