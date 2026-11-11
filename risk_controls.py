"""Deterministic entry guards shared by PAPER and LIVE traders."""
from __future__ import annotations

import math


def assess_new_position(
    state: dict,
    proposed_exposure_usd: float,
    *,
    max_exposure_usd: float,
    max_open_positions: int,
    reserved_exposure_usd: float = 0.0,
    reserved_positions: int = 0,
) -> tuple[bool, str | None]:
    """Decide whether a new position fits configured global risk limits."""
    if not isinstance(state, dict):
        return False, "invalid_position_state"
    if state.get("emergency_stop"):
        return False, "emergency_stop_active"
    values = (proposed_exposure_usd, max_exposure_usd, reserved_exposure_usd)
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) for value in values):
        return False, "invalid_exposure_value"
    if proposed_exposure_usd <= 0 or reserved_exposure_usd < 0:
        return False, "invalid_proposed_exposure"
    if (not isinstance(max_open_positions, int) or isinstance(max_open_positions, bool)
            or max_exposure_usd <= 0 or max_open_positions <= 0):
        return False, "risk_limits_disable_entries"
    if (not isinstance(reserved_positions, int) or isinstance(reserved_positions, bool)
            or reserved_positions < 0):
        return False, "invalid_reserved_positions"

    positions = state.get("open_positions", [])
    if not isinstance(positions, list):
        return False, "invalid_position_state"
    if len(positions) + reserved_positions >= max_open_positions:
        return False, "max_open_positions_reached"
    current = 0.0
    for position in positions:
        if not isinstance(position, dict) or "cost_basis_usd" not in position:
            return False, "invalid_position_exposure"
        try:
            cost = float(position["cost_basis_usd"])
        except (AttributeError, TypeError, ValueError, OverflowError):
            return False, "invalid_position_exposure"
        if not math.isfinite(cost) or cost < 0:
            return False, "invalid_position_exposure"
        current += cost
    if current + reserved_exposure_usd + proposed_exposure_usd > max_exposure_usd:
        return False, "max_open_exposure_reached"
    return True, None

