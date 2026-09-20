"""Safety checks shared by PAPER/LIVE execution paths.

This module is intentionally dependency-free. Call ``validate_order`` immediately
before constructing/sending an order; it fails closed when configuration is unsafe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class OrderLimits:
    max_position_sol: float
    max_daily_loss_sol: float
    max_open_positions: int
    min_balance_reserve_sol: float = 0.05


@dataclass(frozen=True)
class SafetySnapshot:
    balance_sol: float
    daily_pnl_sol: float
    open_positions: int
    already_exposed_sol: float = 0.0


def validate_order(
    amount_sol: float,
    limits: OrderLimits,
    snapshot: SafetySnapshot,
    *,
    trading_mode: str = "PAPER",
    emergency_stop: bool = False,
    idempotency_key: Optional[str] = None,
) -> None:
    """Raise ``ValueError`` if an order must not be submitted.

    The guard is conservative and independent from the strategy score. It is
    suitable for both paper and live execution and prevents accidental LIVE
    orders when the emergency stop is enabled.
    """
    mode = trading_mode.upper().strip()
    if mode not in {"PAPER", "LIVE"}:
        raise ValueError("TRADING_MODE must be PAPER or LIVE")
    if emergency_stop:
        raise ValueError("Emergency stop is enabled")
    if not idempotency_key or not idempotency_key.strip():
        raise ValueError("Missing idempotency key")
    if amount_sol <= 0:
        raise ValueError("Order amount must be positive")
    if amount_sol > limits.max_position_sol:
        raise ValueError("Order exceeds max position size")
    if snapshot.open_positions >= limits.max_open_positions:
        raise ValueError("Maximum number of open positions reached")
    if snapshot.daily_pnl_sol <= -abs(limits.max_daily_loss_sol):
        raise ValueError("Daily loss limit reached")
    remaining = snapshot.balance_sol - snapshot.already_exposed_sol - amount_sol
    if remaining < limits.min_balance_reserve_sol:
        raise ValueError("Minimum SOL reserve would be breached")


def safe_float(value: object, default: float) -> float:
    """Parse an environment/config value without accepting NaN or infinity."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number
