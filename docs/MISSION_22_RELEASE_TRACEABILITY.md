# Mission 22 — Release traceability and audit plan

**Status: `BLOCKED / MANUAL_VERIFICATION_REQUIRED`**
This matrix is preparatory. It does not authorize a merge or release, and it does not claim an independent DEX/CPI audit. The historical [Mission 17 release matrix](MISSION_17_REPORT.md) remains useful context but is not evidence for current Mission 19–22 work.

## Release decision rules

- Release is not ready while any P0 or P1 row is open, unverified, or supported only by a fixture, caller assertion, or offline test.
- Operational rows require evidence tied to the exact candidate commit and target. A 24-hour soak requires 24 continuous wall-clock hours observed on the authorized PAPER target; virtual time and synthetic runs do not count.
- Each PR head and its exact CI run must be rechecked before a release review. A green check on an ancestor or base branch does not transfer to the candidate head.
- A final reviewer must be independent of the author for risk, execution, persistence and DEX/CPI semantics. No matrix row grants permission to trade, sign, submit, spend, deploy, or merge.

## P0 — release blockers requiring zero tolerance

| ID | Requirement | Evidence required | Current state |
|---|---|---|---|
| P0-01 | No reachable real-order, signing, withdrawal or RPC-write path in this PAPER candidate | Static call graph plus refusal tests on the exact head; no signing key or write endpoint in target configuration | NOT REVALIDATED on Mission 19 head |
| P0-02 | Emergency stop active, `TRADING_MODE=PAPER`, `LIVE_EXECUTION_ENABLED=false` | Exact target startup configuration and read-only readiness evidence | NOT RUN |
| P0-03 | Secrets and wallet material absent from code, logs and evidence | Exact-head Gitleaks result and sanitized evidence review | CI REQUIRED |
| P0-04 | No event or position ambiguity can silently create an effect or false accounting | Exact-head UNKNOWN/crash/replay tests plus independent state-machine review | PARTIAL; provider/runtime evidence open |

## P1 — readiness gates

| ID | Requirement | Acceptance evidence | Current state |
|---|---|---|---|
| P1-01 | Helius history replay proves a complete ordered gap before any queued effect is drained | Exact-head tests for interleaved missing indices, pagination, duplicate signatures, conflicting checkpoint tuple, processed watermark captured after subscription ACK, wait until global finalized reaches that watermark, finalized history replay, stop and capacity handling | PR #33 head `d66793d417edd78fac35b448c9effcce9cf9cca7`; CI `45719579840` succeeded. Independent review found fresh streams have no checkpoint bootstrap: a missing-index event or first connection gap leaves that stream fail-closed and paused. This is an operational blocker. RPC-reported completeness is also not independently proved. |
| P1-02 | Stale workers cannot start effects or acknowledge a newer worker’s claim | Schema migration, multi-process lease-expiry test, recovery-generation fencing, effect-boundary fencing and independent review | Generation fencing and schema migration review found no defect. Independent review also found the stop predicate does not interrupt paginated history replay between read calls; see P2-05. Formal GitHub review and target-runtime evidence remain pending. |
| P1-03 | Every recovered checkpoint matches the stored maximum canonical tuple | Exact-head checkpoint-prefix tests and read-only reconciliation of any legacy target state | Local tests pass; legacy target data not inspected |
| P1-04 | SOL/USD mapping, origin, provider time, age, integrity and confidence are independently verified | Pinned official feed ID, selected read-only verification path, freshness/confidence policy and tamper/refusal tests | BLOCKED; adapter fixture is explicitly unverified |
| P1-05 | Signal identity, deduplication, exposure reservation, admission, position and PnL are atomic/idempotent at runtime | Multi-process crash/restart tests on the actual integrated transaction path, with emergency-stop and risk refusal cases | BLOCKED; Mission 18 primitives have no runtime admission call site |
| P1-06 | Position, fee and exit accounting have verified quote provenance and durable idempotency | Source-linked quote receipt, missing/stale quote refusal, duplicate close prevention and ledger reconciliation | BLOCKED |
| P1-07 | PAPER target is stable and recoverable | Mission 20’s completed 24-hour wall-clock soak, clean shutdown, SQLite backup/restore to new paths and post-restore integrity/readiness evidence | NOT RUN; Mission 19 gates remain open |
| P1-08 | DEX/CPI behavior is reviewed semantically | Independent reviewer traces actual supported instruction accounts/program IDs, CPI semantics, account ownership and refusal cases against versioned official DEX sources | NOT DONE; current CPI tests use synthetic fixtures |
| P1-09 | Exact candidate code and dependencies pass required automation | CI on exact PR head: compile, PAPER default, full pytest, critical Ruff, pip-audit, Gitleaks and coverage artifact; read the coverage report before stating a percentage | #30 head `e9334d5…`: run `45701063429` success; #31 current head `488c9c389289945463faa4789e3d28756e46da79`: run `45724713897` success; #32 current pre-ledger head `024ce00eccdb86f93af95803e37958ab8f2bae5f`: run `45722996200` success; #33 head `d66793d…`: run `45719579840` success; #34 head `8fe1651…`: run `45721468111` success. The present ledger edit requires new CI on #32. |
| P1-10 | PR dependency stack and review are explicit | Verified base/head SHA for each PR, no accidental `main` targeting, independent reviews and no unresolved comments | PR refs and draft/unmerged states were checked: #31/#32 target #30; #33 targets #30; #34 targets #33. Formal GitHub reviews are pending. |

## P2 — quality and operability follow-up

| ID | Work item | Acceptance evidence | Current state |
|---|---|---|---|
| P2-01 | Profile the actual local PAPER store and callback path without provider traffic | Reproducible offline benchmark with code revision, environment, load shape, p50/p95/p99 and synthetic-only disclaimer | PR #34 profiles the actual M19 SQLite inbox with 1,000 synthetic events on code SHA `29f2ede986b8467ae4e29ba8a971fd5bc7389690`; report: `MISSION_21_OFFLINE_PROFILE.md`. It does not measure the full bot, callbacks, RPC or provider cost. |
| P2-02 | Verify privacy and backpressure for dashboard, Telegram and metrics | Redaction and queue-saturation tests with no wallet/signature values in logs or exports | PARTIAL; not an operational proof |
| P2-03 | Reconcile reports and CI references | Confirm every report’s SHA, run ID and changed-file list; fix any stale predecessor evidence | Current PR/head/run ledger below records Missions 18–22 preparations. M18 historical release references still need a separate historical reconciliation. |
| P2-04 | Review unresolved historical state and alert semantics | Redacted state inventory, UNKNOWN handling and operator action records | NOT RUN |
| P2-05 | Stop address-history replay promptly after shutdown is requested | Check the stop predicate between every paginated history read; verify with a deterministic multi-page fixture | Open: recovery checks stop before and after replay, but replay can issue further read-only RPC calls until pagination ends; each call has a 15-second timeout. No effect drain follows the stop. |

## Exact-head review ledger

Complete one row per candidate PR. Do not reuse an ancestor’s CI run or another PR’s review.

| Mission / PR | Base branch + SHA | Head branch + SHA | Exact CI run + result | Independent reviewers | Draft / merged | Decision |
|---|---|---|---|---|---|---|
| 18 / #30 | `codex/mission-17-final-paper-rc-20261125` / `1698ae1116dd28d17e9264abb3145c383c3dd5dd` | `codex/mission-18-core-paper-integration-20261125` / `e9334d5716b7fd5969cae3f077bb9fc1a2a1d1d7` | `45701063429` / success on this head | Formal GitHub review pending | Draft / unmerged | PARTIALLY_READY; manual integration gates remain |
| 19 / #33 | `codex/mission-18-core-paper-integration-20261125` / `e9334d5716b7fd5969cae3f077bb9fc1a2a1d1d7` | `codex/mission-19-data-integrity-20261125` / `d66793d417edd78fac35b448c9effcce9cf9cca7` | `45719579840` / success on this head | Three read-only Codex reviews: one found the fail-closed fresh-stream bootstrap P1 and pagination-stop P2; price provenance review found no P0/P1/P2 and one non-blocking clarity note. Formal GitHub review pending. | Draft / unmerged | PARTIALLY_READY / MANUAL_VERIFICATION_REQUIRED |
| 20 prep / #31 | `codex/mission-18-core-paper-integration-20261125` / `e9334d5716b7fd5969cae3f077bb9fc1a2a1d1d7` | `codex/mission-20-readiness-prep-20261125` / `488c9c389289945463faa4789e3d28756e46da79` | `45724713897` / success on this head | Independent prior-head review found CLI exit-code P2, corrected in code/tests commit `ffa30265d790391f00bcfadce14f254179c0c442`; final-head follow-up found no new finding (9 targeted tests passed) | Draft / unmerged | Offline evidence-shape validator and operator template; target soak/restore not run, M19 gate blocks execution |
| 21 / #34 | `codex/mission-19-data-integrity-20261125` / `d66793d417edd78fac35b448c9effcce9cf9cca7` | `codex/mission-21-paper-profiler-20261125` / `8fe16516d013242cf36cbfb2b07affb9bc8dc99a` | `45721468111` / success on this head | Independent read-only review: no P0/P1; P2 notes the public profile function relies on caller-provided directory isolation, while CLI uses a fresh temporary directory | Draft / unmerged | Offline component profile only; operational evidence absent |
| 22 / #32 | `codex/mission-18-core-paper-integration-20261125` / `e9334d5716b7fd5969cae3f077bb9fc1a2a1d1d7` | `codex/mission-22-release-matrix-20261125` / `024ce00eccdb86f93af95803e37958ab8f2bae5f` before this ledger update | `45722996200` / success on the recorded head before this update | Scope/gate audit; no formal GitHub review | Draft / unmerged | Preparatory traceability only; this ledger update requires a new exact-head CI run |

## DEX/CPI review scope

The current fixture-based trace validator can demonstrate that selected synthetic traces are refused or accepted by the existing checks. It does not prove that every supported DEX instruction, router version, account layout, CPI, token extension, upgradeable program, or deployed program behaves safely. A future independent audit must record the official source and revision for each supported route, identify the exact account and program assumptions, add negative fixtures for unsupported/ambiguous traces, and state what was not audited. Do not describe static fixtures as an independent semantic audit.

## Current release status

**No final release decision is available.** Mission 19’s fresh-stream checkpoint bootstrap, provider integrity, SOL/USD accounting, and runtime integration gates remain open; shutdown may also permit further read-only history-page requests until a pagination loop completes. Mission 20’s 24-hour target soak and target restoration evidence do not exist; the M20 PR is an operator template only. Mission 21 has one synthetic local SQLite inbox profile, not current-bot or RPC capacity evidence. Mission 22’s independent DEX/CPI semantic audit, formal reviews, current exact-head CI for PR #34, and operational evidence remain outstanding. This document is an acceptance ledger, not an approval.
