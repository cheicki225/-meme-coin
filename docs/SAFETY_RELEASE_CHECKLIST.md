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
- Every LIVE sell now persists a per-token `safety_pending_sell_tokens` intent before submission; a restart or ambiguous outcome leaves the token blocked until on-chain reconciliation.
- A confirmed sell intent remains `confirmed_awaiting_accounting` until the position/PnL update is saved; only then is the reservation released.
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
- The GoPlus adapter parses the documented Solana schema (mintable/freezable/closable authorities, default account state, non-transferability, transfer hooks/fees, creator risk and DEX TVL); it does not infer EVM honeypot or ownership-renunciation properties from unrelated fields.

Known limitations (not represented as completed capabilities):
- SPL migration detection is heuristic and does not prove common ownership; it intentionally does not auto-add destinations.
- Front Run Sell remains polling-based/best-effort, not same-block execution.
- Auto-Sell on Big Buy remains an approximate pool-transaction heuristic.
- A read-only DexScreener pool-liquidity monitor now samples open positions at a bounded cadence and alerts on same-pool liquidity drops or pool-address changes. It is heuristic, depends on DexScreener indexing, and does not verify LP locks, token vault balances, or all Solana DEX programs; it never auto-sells.
- No deterministic rug-proof classifier is implemented.

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
- [x] Persist LIVE sell intent before network submission and release it only after position/accounting persistence; cover restart-safe blocking with mocked tests.
- [x] Fail closed when enabled anti-rug provider checks or configured developer-behavior data are unavailable.
- [x] Add network-free reconciliation helpers for intents left in `unknown` state.

- [x] Add read-only same-pool liquidity-drop and pool-change alerts with bounded sampling and regression tests; alerts do not trigger automatic sells.
- [x] Add operator-reviewed recovery helpers for unresolved LIVE sell intents; confirmed sells remain blocked until accounting is reconciled.
- [ ] Run the complete repository test suite (the dedicated `tests/` CI suite passes; root-level/manual checks remain to be audited).
- [ ] Run PAPER mode soak testing before any LIVE transaction.
- [ ] Keep LIVE disabled until all unchecked items above are completed.

LIVE must remain disabled while this checklist has unchecked items.

## Operator sell-intent recovery

`intent_reconciliation.list_unresolved_sell_intents(state)` lists pending, unknown, and confirmed-but-unaccounted sell records without changing them. Before resolving an item, an operator must inspect the transaction signature and the wallet's token/SOL balances on a trusted Solana explorer or RPC source.

- If chain evidence proves the transaction failed and no token debit occurred, call `resolve_sell_intent(state, mint, status="failed", note="...verified evidence...")`, then persist the data store. This releases the pending reservation only after an explicit operator decision.
- If chain evidence proves the sell succeeded, call `resolve_sell_intent(state, mint, status="confirmed", signature="<verified signature>", note="...verified evidence...")`, then persist the data store. The mint remains blocked in `confirmed_awaiting_accounting`; do not clear it until position units, SOL proceeds, realized PnL, and transaction signature have been reconciled and saved.
- If chain outcome is ambiguous, do not call the resolver, do not retry, and leave the token blocked.
- Never infer failure solely from a client timeout or missing confirmation response.
