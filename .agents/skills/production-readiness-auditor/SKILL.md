---
name: production-readiness-auditor
description: Audit evidence for PAPER and release readiness in this Solana bot, separating implementation, CI, deployment checks, and LIVE blockers.
---

# Production Readiness Auditor

## Déclencheurs

Use this Skill when the user asks whether the bot, a release, a pull request, or a named subsystem is ready to operate, deploy, or move beyond PAPER. Do not invoke it for an ordinary code change unless readiness is part of the request.

## Audit avant toute modification

1. Treat readiness as a read-only evidence review unless the user separately asks for a concrete change.
2. Inspect the current commit, branch, worktree, open pull-request base/head, CI runs, and changed files. Re-read SECURITY.md, LIVE_READINESS.md, TESTING.md, PROGRESS.md, and the feature-specific evidence at that commit.
3. Verify that reported tests and benchmark artifacts belong to the current commit or explicitly identify their older commit. Do not treat historical documentation as current proof.
4. Identify what evidence is missing before suggesting more work. Do not contact providers, deployment environments, wallets, or Telegram during this audit.

## Workflow

- Report separate statuses for code present, offline tests, CI, read-only provider checks, PAPER operation in the target environment, recovery/restore exercises, and LIVE readiness.
- Use the repository's established status vocabulary where applicable. Mark a claim BLOCKED or MANUAL VERIFICATION REQUIRED when required evidence is absent; do not infer success from implementation, test count, coverage, or a synthetic soak.
- Check the documented gates for transaction security, ambiguous signatures, RPC/WebSocket capability and quotas, Telegram callback authorization, accounting/PnL, risk controls, recovery, and operational rollback.
- If a gap is independently fixable within the user's request, hand it to the relevant specialist. Otherwise provide an ordered list of concrete evidence needed and its owner.
- Keep conclusions scoped to the reviewed commit and environment. Explicitly state what was not examined.

## Sécurité

Keep PAPER as the operating default. Never activate LIVE, sign or submit a transaction, use a funded wallet, inspect secrets, or interpret configuration presence as authorization. Preserve the current RPC endpoint defaults and never disclose endpoint strings.

## Tests

Do not create a new readiness claim by running synthetic tests. When the user requests validation, run only appropriate offline tests or CI evidence checks and report the exact command and commit. Do not initiate RPC, Telegram, deployment, or wallet checks without explicit authorization for that separate operation.

## Preuves attendues

Link each conclusion to a current code path, test, CI run, artifact, or operator record. Label synthetic loopback measurements as synthetic; distinguish unit tests from provider checks and deployment soak. State coverage with its denominator and commit when available.

## Limites

A green CI, a high coverage percentage, a benchmark, or the existence of LIVE code does not establish deployment readiness. Current repository documents say LIVE is BLOCKED, public RPC behavior is limited, no target-environment PAPER soak or restore is evidenced, and CPI/DEX semantics remain independently unreviewed. Re-check those facts at the commit under review.

## Passages de relais

Use jupiter-transaction-security for transaction and CPI/DEX findings; solana-rpc-performance for provider and latency evidence; trading-risk-manager for limits and stops; sqlite-recovery for restart and persistence; pnl-accounting and backtesting-validation for financial evidence; python-testing-ci for reproducible CI gaps; project-master for multi-domain remediation planning.