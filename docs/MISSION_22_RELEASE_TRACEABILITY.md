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
| P1-01 | Helius history replay proves a complete ordered gap before any queued effect is drained | Exact-head tests for interleaved missing indices, pagination, duplicate signatures, conflicting checkpoint tuple, finalized reconnect boundary, subscription ACK and capacity overflow | Mission 19 local synthetic cases pass; provider completeness and final independent review unverified |
| P1-02 | Stale workers cannot start effects or acknowledge a newer worker’s claim | Schema migration, multi-process lease-expiry test, recovery-epoch fencing, effect-boundary fencing and independent review | Mission 19 local code/tests present; exact-head CI and final independent review pending |
| P1-03 | Every recovered checkpoint matches the stored maximum canonical tuple | Exact-head checkpoint-prefix tests and read-only reconciliation of any legacy target state | Local tests pass; legacy target data not inspected |
| P1-04 | SOL/USD mapping, origin, provider time, age, integrity and confidence are independently verified | Pinned official feed ID, selected read-only verification path, freshness/confidence policy and tamper/refusal tests | BLOCKED; adapter fixture is explicitly unverified |
| P1-05 | Signal identity, deduplication, exposure reservation, admission, position and PnL are atomic/idempotent at runtime | Multi-process crash/restart tests on the actual integrated transaction path, with emergency-stop and risk refusal cases | BLOCKED; Mission 18 primitives have no runtime admission call site |
| P1-06 | Position, fee and exit accounting have verified quote provenance and durable idempotency | Source-linked quote receipt, missing/stale quote refusal, duplicate close prevention and ledger reconciliation | BLOCKED |
| P1-07 | PAPER target is stable and recoverable | Mission 20’s completed 24-hour wall-clock soak, clean shutdown, SQLite backup/restore to new paths and post-restore integrity/readiness evidence | NOT RUN; Mission 19 gates remain open |
| P1-08 | DEX/CPI behavior is reviewed semantically | Independent reviewer traces actual supported instruction accounts/program IDs, CPI semantics, account ownership and refusal cases against versioned official DEX sources | NOT DONE; current CPI tests use synthetic fixtures |
| P1-09 | Exact candidate code and dependencies pass required automation | CI on exact PR head: compile, PAPER default, full pytest, critical Ruff, pip-audit, Gitleaks and coverage artifact; read the coverage report before stating a percentage | CI REQUIRED on candidate head |
| P1-10 | PR dependency stack and review are explicit | Verified base/head SHA for each PR, no accidental `main` targeting, independent reviews and no unresolved comments | NOT REVALIDATED for Missions 19–22 |

## P2 — quality and operability follow-up

| ID | Work item | Acceptance evidence | Current state |
|---|---|---|---|
| P2-01 | Profile the actual local PAPER store and callback path without provider traffic | Reproducible offline benchmark with code revision, environment, load shape, p50/p95/p99 and synthetic-only disclaimer | Historical Mission 6 benchmark is not this runtime path; Mission 21 prep pending |
| P2-02 | Verify privacy and backpressure for dashboard, Telegram and metrics | Redaction and queue-saturation tests with no wallet/signature values in logs or exports | PARTIAL; not an operational proof |
| P2-03 | Reconcile reports and CI references | Confirm every report’s SHA, run ID and changed-file list; fix any stale predecessor evidence | Mission 18 report has a historical CI run reference to reconcile |
| P2-04 | Review unresolved historical state and alert semantics | Redacted state inventory, UNKNOWN handling and operator action records | NOT RUN |

## Exact-head review ledger

Complete one row per candidate PR. Do not reuse an ancestor’s CI run or another PR’s review.

| Mission / PR | Base branch + SHA | Head branch + SHA | Exact CI run + result | Independent reviewers | Draft / merged | Decision |
|---|---|---|---|---|---|---|
| 18 / #30 | `codex/mission-17-final-paper-rc-20261125` / `1698ae1116dd28d17e9264abb3145c383c3dd5dd` | `codex/mission-18-core-paper-integration-20261125` / `e9334d5716b7fd5969cae3f077bb9fc1a2a1d1d7` | `45701063429` / success on recorded head | Pending formal GitHub review | Draft / unmerged at last check | Review only |
| 19 | #30 exact head `e9334d5716b7fd5969cae3f077bb9fc1a2a1d1d7` | Mission 19 head and PR not yet finalized | Local test suite passed before commit; exact-head CI pending | Three Codex agents used; final independent recovery review pending | Not created | PARTIALLY_READY |
| 20 prep | #30 head `e9334d5716b7fd5969cae3f077bb9fc1a2a1d1d7` | Mission 20 documentation head pending | Diff check only; no target run | Mission 20–22 scope audit by Codex agent | Draft PR pending | Preparatory only |
| 21 prep | Mission 19 head required | Mission 21 profile head pending | Offline profile/tests pending | Pending | Draft PR pending | Preparatory only |
| 22 prep | #30 head `e9334d5716b7fd5969cae3f077bb9fc1a2a1d1d7` | Mission 22 documentation head pending | Diff check only; no release test | Mission 20–22 scope audit by Codex agent | Draft PR pending | Preparatory only |

## DEX/CPI review scope

The current fixture-based trace validator can demonstrate that selected synthetic traces are refused or accepted by the existing checks. It does not prove that every supported DEX instruction, router version, account layout, CPI, token extension, upgradeable program, or deployed program behaves safely. A future independent audit must record the official source and revision for each supported route, identify the exact account and program assumptions, add negative fixtures for unsupported/ambiguous traces, and state what was not audited. Do not describe static fixtures as an independent semantic audit.

## Current release status

**No final release decision is available.** Mission 19’s provider and runtime integration gates are open; Mission 20’s target soak and restoration evidence do not exist; Mission 21 has no current-runtime performance evidence; Mission 22’s independent DEX/CPI audit and exact-head reviews remain outstanding. This document is an acceptance ledger, not an approval.
