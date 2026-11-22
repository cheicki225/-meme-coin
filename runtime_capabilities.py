"""Report provider capabilities without exposing configured endpoint URLs."""


def assess_provider_startup(trading_mode: str, helius_api_key: str) -> dict:
    """Describe the current runtime path without assuming key validity/quota."""
    if helius_api_key:
        return {
            "status": "credentials_configured",
            "can_start": True,
            "message": "Authenticated Helius features are configured but not health-verified.",
        }

    if str(trading_mode).strip().upper() == "LIVE":
        return {
            "status": "blocked_live",
            "can_start": False,
            "message": "LIVE requires configured Helius WebSocket and enhanced transaction parsing.",
        }

    return {
        "status": "degraded_public_http",
        "can_start": True,
        "message": (
            "PAPER can use the configured HTTP RPC, including a public endpoint; "
            "Helius WebSocket discovery, copy trading, protection streaming, and "
            "enhanced transaction parsing are unavailable."
        ),
    }
