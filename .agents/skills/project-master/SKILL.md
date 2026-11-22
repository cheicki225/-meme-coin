---
name: project-master
description: Coordinate cross-domain engineering and review tasks in this Solana trading bot, including branch-aware integration and evidence-based handoffs.
---

# Project Master

## Déclencheurs

Use this Skill when a request spans multiple bot domains, changes shared architecture, needs a coordinated audit or release plan, or asks which specialist work should happen next. For a focused change in one domain, route directly to its specialist.

## Audit avant toute modification

1. Read the current branch and worktree state with read-only Git commands. Inspect local edits, untracked files, remote branches, and relevant open pull requests before selecting a base.
2. Never assume that an open pull request has been merged. For stacked work, identify and verify the exact base and head commit; create a dedicated branch from the latest relevant unmerged head when the request requires integration.
3. Inspect AGENTS.md if present, plus SECURITY.md, ARCHITECTURE.md, TESTING.md, and the documents and tests for the requested features. Do not read local secret files or expose credential-bearing RPC URLs.
4. Before a multi-file change, write down the requested outcome, owned files, safety effects, proof required, and a reversible recovery path.

## Workflow

- Split the request into domain-sized outcomes and choose one primary owner for each file. Delegate only independent work when subagents are available and useful; never assign concurrent edits to the same file.
- Route each domain to the matching Skill in SKILLS_CATALOG.md. Keep the project master responsible for scope, dependency order, integration, and the final evidence report.
- Integrate after reviewing each draft or patch against the current code and user scope. Do not merge older pull requests or silently rebase onto main.
- Prefer offline, deterministic work. If current external technical facts are necessary to resolve a real blocker, first check whether web-research-problem-solver is available. Use it only for that sourced investigation; report unavailability instead of simulating research.
- Finish by checking the branch diff, changed paths, and requested validation. Report what is implemented separately from what tests, CI, read-only provider checks, PAPER operation, or production operation actually proved.

## Sécurité

- Keep PAPER as the default and preserve the current public RPC configuration and fallback order.
- Do not sign, submit, or broadcast transactions, enable LIVE, or use real wallets or funds. A code path or a configuration option is not permission to exercise it.
- Never request, read, print, log, or commit private keys, API keys, bot tokens, or credential-bearing URLs.
- Do not change risk thresholds, exposure rules, emergency-stop behavior, or transaction authorization without explicit user authorization.

## Tests

Use the relevant offline tests from TESTING.md for code changes. Keep transaction, RPC, Telegram, and recovery tests mocked or isolated. For documentation-only work, validate the manifests, links, routing examples, and diff instead of claiming bot behavior was tested. Do not run unrelated tests merely to increase a count.

## Preuves attendues

Record the commit, base branch, files changed, commands actually run, and results. Separate static review, structural validation, offline or simulated tests, read-only provider checks, and real PAPER operation. A syntax or structure check alone does not show that Codex selected or followed a Skill correctly.

## Limites

Skills provide task guidance; they do not authorize network access, wallet use, transaction signing, LIVE operation, deployment, or a pull-request merge. The current project documentation marks LIVE as BLOCKED and PAPER soak in the deployment environment as unverified.

## Passages de relais

Use the specialist selected in SKILLS_CATALOG.md for RPC, Jupiter, token risk, wallet collection, copy trading, risk controls, multi-wallet execution, accounting, backtesting, Telegram, CI, SQLite recovery, or competitive benchmarking. Hand broad readiness decisions to production-readiness-auditor.