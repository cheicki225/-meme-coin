# Mission 17 PAPER release candidate runbook

**Status: `MANUAL_VERIFICATION_REQUIRED`**
This runbook records safe local procedures. No bot, provider, RPC, Telegram, wallet, or 24-hour soak was started during Mission 17.

## Candidate limits

- Keep `TRADING_MODE=PAPER`, `LIVE_EXECUTION_ENABLED=false`, and the emergency stop active.
- The example config at [`.env.paper.example`](../.env.paper.example) contains blank credential fields. It is not loaded automatically and is not a runnable soak configuration.
- Do not add a signing wallet, private key, Jito tip, or transaction submission endpoint. No order, signed transaction, or RPC write is part of this release candidate.
- Helius live notifications currently lack the canonical transaction index. The first such event is durably marked `UNKNOWN`, which pauses that stream. No local-arrival ordering is accepted.
- USD valuation is disabled until provider-side freshness and independent provenance are verified. Existing positions and manual exits can remain `UNKNOWN`; do not infer a fill or PnL from a stored quote.
- A `LOCAL_CHECKS_READY` result from the read-only script below covers only local flags and SQLite state. It is not provider validation, runtime readiness, or soak evidence.

## Local health and readiness

Run the read-only check from the repository root:

```powershell
$env:TRADING_MODE = 'PAPER'
$env:LIVE_EXECUTION_ENABLED = 'false'
python scripts/paper_release_readiness.py `
  --state-db sniper_data.sqlite3 `
  --helius-db data/helius_events.sqlite3
```

The script performs only SQLite read-only integrity checks and inspects persisted safety flags. It requires explicit mode variables in its process environment and does not infer missing values or read `.env`; the same exported variables must be used for any later process. It does not display secrets, contact a provider, or modify state. It reports `BLOCKED` for LIVE flags, an inactive emergency stop, pending transaction signatures, corrupted SQLite, an unresolved or recovery-paused Helius stream, malformed persisted Helius order/status/recovery-flag values, events without canonical slot/index, or UNKNOWN position exits. Missing databases, old Helius schemas, malformed column/state structure and open positions without an offline-verifiable monitor heartbeat remain `MANUAL_VERIFICATION_REQUIRED`.

The local dashboard is also read-only and binds to `127.0.0.1`:

```powershell
python paper_dashboard.py `
  --state-path sniper_data.sqlite3 `
  --helius-db data/helius_events.sqlite3 `
  --metrics-file data/execution_metrics.jsonl
```

Open the printed loopback URL, then stop with **Ctrl+C**. Runtime process health and provider health remain `UNKNOWN` when they cannot be read from persisted evidence. The page has no trading controls.

For offline code checks, use Python 3.11 and the repository CI dependency set. These checks do not start the bot:

```powershell
$env:PYTHON_DOTENV_DISABLED = 'true'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -m compileall -q .
python -m pytest -q
```

Run these checks in a separate PowerShell session. `PYTHON_DOTENV_DISABLED` is only for offline validation; remove or restore it before any later bot process so the approved process configuration can load as intended.

## Start, stop, and restart

Mission 17 did not start `main.py`; the current candidate is not ready for a target PAPER soak because live event ordering and verified USD valuation remain blocked. Do not start it against a provider as part of this runbook until an authorized target and read-only provider configuration have been explicitly established.

When that gate is cleared by a later reviewed change:

1. Confirm the exact checkout and commit, `TRADING_MODE=PAPER`, `LIVE_EXECUTION_ENABLED=false`, active emergency stop, and authorized read-only provider settings.
2. Run the readiness command above against the target's state and event databases. Resolve every `BLOCKED`/`UNKNOWN` item through a documented reconciliation; do not clear rows or pause flags manually.
3. Start `python main.py` only in that authorized PAPER environment. Keep the console and local dashboard available for operator observation.
4. Stop with **Ctrl+C** and wait for process shutdown before backing up SQLite. Avoid force-killing a process during a database write.
5. Before restart, repeat the readiness check and reconcile every pending or UNKNOWN event/position. Restart only after the owning gate has a verified resolution.

The current event store has no safe automatic resolution for a live event that became `UNKNOWN` because its canonical index was missing. Preserve the database and request a code-reviewed reconciliation procedure; do not delete the row, reset its status, or move a checkpoint by hand.

## Backups and restoration

1. Stop the bot cleanly and confirm its process has exited.
2. Create timestamped backup files in a separate backup directory using SQLite's backup API. Do this separately for `sniper_data.sqlite3` and `data/helius_events.sqlite3`; do not copy a live WAL database file by file.
3. Run `PRAGMA integrity_check` against each backup using a read-only connection. Record the UTC time, source path, backup path, and result; do not include wallet addresses or secrets in the record.
4. Restore only to a new candidate path, verify integrity and state, then point a separately reviewed target configuration at that copy. Keep the source and backup unchanged until an operator confirms the restored state.
5. If the restored copy contains pending/UNKNOWN events, preserve their status and perform explicit reconciliation before resuming.

Example read-only integrity check for a backup path:

```powershell
python -c "import sqlite3; db=sqlite3.connect('file:sniper_data.backup.sqlite3?mode=ro', uri=True); print(db.execute('PRAGMA integrity_check').fetchall()); db.close()"
```

Choose unique backup filenames. Do not run a restore over the original database.

## Reconciliation and alerts

- Treat Helius `UNKNOWN`, `intake_paused`, queue-full, and missing canonical order as stop conditions for that stream. No later event in the stream is admitted while the outcome is unresolved.
- Treat an open position marked `exit_status=unknown` as requiring manual review. Preserve units and cost basis; do not fabricate an exit or PnL.
- Treat missing/stale provider price, missing transaction index, invalid GoPlus response, SQLite integrity failure, inactive emergency stop, and RPC 429/5xx as readiness failures or unknowns. Do not clear them to resume.
- Existing RPC transaction submission is one-shot and remains `UNKNOWN` on an ambiguous response; do not retry a submission to “check” whether it landed.
- Review the SQLite integrity result, event-store queue/paused state, emergency-stop flag, and provider quota telemetry before every restart. The dashboard does not prove that a runtime worker is alive.
- The sample rate limits are `SOLANA_RPC_REQUESTS_PER_SECOND=3` and `HELIUS_ENHANCED_REQUESTS_PER_SECOND=1`. These are configured defaults, not verified account entitlements. Confirm the authorized provider's current quota before any read-only soak; preserve retry/backoff caps and do not raise request rates to force recovery.

## 24-hour PAPER soak procedure (not executed)

The soak remains `MANUAL_VERIFICATION_REQUIRED` because no authorized target environment, provider entitlement, or canonical live-index feed was established for this mission. Do not substitute synthetic tests or accelerated time for elapsed runtime evidence.

After a future change makes readiness gates pass, an operator may conduct a soak only in a specifically authorized target with no signing key and no transaction submission capability:

1. Record the exact commit, host, configuration fingerprint excluding secret values, UTC start time, emergency-stop state, SQLite integrity results, provider quota, and dashboard snapshot.
2. Run the PAPER process for at least 24 continuous wall-clock hours. Retain timestamped process heartbeat, queue/checkpoint/UNKNOWN state, provider request outcome and rate-limit counts, monitor state, and shutdown/restart observations.
3. Stop immediately on any live/unverified order path, non-PAPER flag, inactive emergency stop, missing canonical transaction index, stale/unauthenticated valuation, UNKNOWN effect, SQLite error, or quota breach. Preserve state and record the UTC stop time and reason.
4. At 24 hours, stop cleanly, back up both databases, verify integrity, reconcile events/positions, and compare the opening/closing snapshots. No claim of profitability or DEX/CPI semantic correctness follows from this soak.

This procedure has not been performed. No external DEX/CPI semantic audit, provider contract validation, or production audit is claimed. Those remain separate release gates.
