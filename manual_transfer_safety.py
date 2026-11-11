"""Policy checks for the separate Telegram-initiated SOL transfer path."""
from __future__ import annotations

import math


def validate_manual_transfer(mode: str, emergency_stop: bool, has_pending: bool,
                             amount_sol: float, maximum_sol: float, *,
                             execution_enabled: bool = False) -> None:
    if str(mode).upper() != "LIVE":
        raise ValueError("Manual SOL transfers are disabled while TRADING_MODE=PAPER.")
    if not execution_enabled:
        raise ValueError("Manual SOL transfers require LIVE_EXECUTION_ENABLED=true.")
    if emergency_stop:
        raise ValueError("Emergency stop is active; manual SOL transfers are blocked.")
    if has_pending:
        raise ValueError("A transaction signature is unresolved; reconcile it before transferring.")
    if not isinstance(amount_sol, (int, float)) or isinstance(amount_sol, bool) or not math.isfinite(amount_sol):
        raise ValueError("Transfer amount must be a finite number.")
    if (not isinstance(maximum_sol, (int, float)) or isinstance(maximum_sol, bool)
            or not math.isfinite(maximum_sol) or maximum_sol <= 0):
        raise ValueError("Manual transfer cap must be finite and positive.")
    if amount_sol <= 0 or amount_sol > maximum_sol:
        raise ValueError(f"Transfer amount must be greater than zero and at most {maximum_sol:g} SOL.")

