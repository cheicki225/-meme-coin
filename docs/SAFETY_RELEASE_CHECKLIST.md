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
- Confirmation timeouts are explicitly classified as `unknown` (not definite failures); live buy and MultiBuy persist that state and do not retry automatically.
- An ambiguous LIVE sell blocks further sells for that token until operator reconciliation, preventing repeated sell submissions on each monitoring poll.
- SPL token decimals are verified strictly; missing, malformed, boolean, or out-of-range metadata blocks conversion rather than assuming 6 decimals.
- Network-free reconciliation helpers list unresolved intents and apply explicit operator-confirmed resolutions.
- Automated pytest checks in GitHub Actions include guard + runtime/idempotency + reconciliation tests.

## Developer migration, auto-sell and anti-rug safeguards

- SPL token transfers sent by monitored `track_creation` developer wallets are recorded as migration candidates with mint/signature evidence.
- Two distinct transferred mints or three independent transfer signatures escalate a candidate to `suspected_migration`; alerts are deduplicated and evidence is bounded.
- Migration candidates are advisory only. Recipients are not auto-added or auto-traded because a token transfer alone does not prove wallet ownership or migration.
- Copy-sell supports a per-wallet `copy_sell_pct` (default 100%) and validates the percentage before selling.
- Sell Initials now routes through the same execution primitive as every other sell; it no longer mutates a LIVE position without submitting a sell.
- PAPER and LIVE sell accounting use a single returned SOL-proceeds value, avoiding a second price lookup after a confirmed sell.
- A confirmed LIVE sell that cannot be accounted is persisted in `safety_unaccounted_confirmed_sells`; further sells for that mint are blocked pending reconciliation.
- Unexpected LIVE sell execution errors are persisted as `unknown`; no automatic retry is permitted until the transaction is reconciled.
- Partial sell ratios and token unit amounts are validated before execution.
- When GoPlus checks are enabled, missing credentials, API errors, malformed responses, and unindexed tokens fail closed. An enabled developer-behavior filter also blocks if its required data cannot be obtained.
- GoPlus `is_open_source` is treated as source verification, not as proof that mint/freeze authorities were renounced.

Known limitations (not represented as completed capabilities):
- SPL migration detection is heuristic and does not prove common ownership; it intentionally does not auto-add destinations.
- Front Run Sell remains polling-based/best-effort, not same-block execution.
- Auto-Sell on Big Buy remains an approximate pool-transaction heuristic.
- No comprehensive LP-liquidity lock/migration monitor or deterministic rug-proof classifier is implemented.

## Required before LIVE activation

- [x] Call `validate_order()` from the actual LIVE buy path before every swap.
- [x] Call `validate_order()` for each individual MultiBuy leg.
- [x] Build the safety snapshot from the actual wallet balance and persisted open positions.
- [x] Persist and enforce idempotency keys across process restarts.
- [x] Add an explicit, independently verified emergency-stop source.
- [x] Add mocked RPC integration tests for the LIVE safety preflight (emergency stop + malformed balance fail closed).
- [x] Add fail-closed validation for malformed/inconsistent Jupiter quotes before transaction build/sign/send, with tests.
- [x] Treat post-submission confirmation timeouts as ambiguous and block automatic buy retries, with tests.
- [x] Block repeated sells for a token after an ambiguous confirmation timeout, with a mocked regression test.
- [x] Fail closed when SPL token decimals cannot be verified, with tests.
- [x] Persist confirmed buy signature and raw amounts when post-confirmation accounting fails; block further buys for the mint until reconciliation, with regression tests.
- [x] Prevent executor code from downgrading terminal intents; restrict operator resolution to unresolved intents and verified outcomes, with regression tests.
- [x] Detect and persist SPL transfer-based developer migration candidates with deduplication and escalation tests.
- [x] Execute Copy Sell percentages through the sell primitive; route Sell Initials through real execution; test both.
- [x] Persist confirmed sell accounting failures and unexpected sell outcomes; block automatic repeat sells.
- [x] Fail closed when enabled anti-rug provider checks or configured developer-behavior data are unavailable.
- [x] Add network-free reconciliation helpers for intents left in `unknown` state.
- [ ] Run the complete repository test suite (the dedicated `tests/` CI suite passes; root-level/manual checks remain to be audited).
- [ ] Run PAPER mode soak testing before any LIVE transaction.
- [ ] Keep LIVE disabled until all unchecked items above are completed.

LIVE must remain disabled while this checklist has unchecked items.
