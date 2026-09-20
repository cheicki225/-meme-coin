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
- Persistent order-intent registry with pending/confirmed/failed/unknown states.
- Actual LIVE buy path calls the safety preflight before Jupiter/Jito submission.
- Each MultiBuy leg is preflighted against its execution wallet balance.
- Live safety snapshot reads confirmed wallet balance and persisted open-position exposure.
- Independent emergency-stop source via `BOT_EMERGENCY_STOP` plus config/runtime setting.
- Ambiguous executor failures are kept blocked to prevent duplicate buys after restart.
- Automated pytest checks in GitHub Actions include guard + runtime/idempotency tests.

## Required before LIVE activation

- [x] Call `validate_order()` from the actual LIVE buy path before every swap.
- [x] Call `validate_order()` for each individual MultiBuy leg.
- [x] Build the safety snapshot from the actual wallet balance and persisted open positions.
- [x] Persist and enforce idempotency keys across process restarts.
- [x] Add an explicit, independently verified emergency-stop source.
- [ ] Add mocked RPC/Jupiter integration tests for `live_trader.py`.
- [ ] Add fail-closed validation for malformed/partial Jupiter responses.
- [ ] Add reconciliation tooling for intents left in `unknown` state.
- [ ] Run the complete repository test suite.
- [ ] Run PAPER mode soak testing before any LIVE transaction.
- [ ] Keep LIVE disabled until all unchecked items above are completed.

LIVE must remain disabled while this checklist has unchecked items.
