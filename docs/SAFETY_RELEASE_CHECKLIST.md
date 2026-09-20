# Safety release checklist

This checklist is required before enabling LIVE trading.

## Implemented

- Fail-closed order validation for PAPER/LIVE.
- Position-size limit.
- Daily-loss limit.
- Maximum open-position limit.
- Minimum SOL reserve.
- Emergency-stop validation.
- Required idempotency key.
- NaN/infinity-safe numeric parsing.
- Automated pytest checks in GitHub Actions.

## Required before LIVE activation

- [ ] Call `validate_order()` from the actual LIVE buy path before every swap.
- [ ] Call `validate_order()` for each individual MultiBuy leg.
- [ ] Build the safety snapshot from the actual wallet balance and persisted open positions.
- [ ] Persist and enforce idempotency keys across process restarts.
- [ ] Add an explicit, independently verified emergency-stop source.
- [ ] Run the complete test suite with mocked RPC/Jupiter responses.
- [ ] Run PAPER mode soak testing before any LIVE transaction.
- [ ] Keep LIVE disabled until all items above are checked.

The safety module is deliberately not treated as LIVE protection until the execution-path integration has been verified.
