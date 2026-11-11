# PAPER soak test runbook

This runbook is a release gate, not evidence that a soak has already run.

## Non-negotiable environment

- `TRADING_MODE=PAPER`
- `BOT_EMERGENCY_STOP=1` (do not disable it as part of this runbook)
- No wallet private key or signing secret is required for a read-only/monitoring soak.
- Configure a read-only Solana RPC/Helius endpoint and any optional read-only data-provider credentials through the deployment secret manager. Never paste credentials into logs, issues, or this repository.
- Confirm the deployed image/commit matches the reviewed branch head before collecting results.

## Run duration and observations

1. Start the bot in the target runtime with the above safety settings.
2. Observe for at least 24 continuous hours; prefer 72 hours across differing network activity.
3. Record process restarts, uncaught exceptions, RPC/provider errors, WebSocket disconnect/reconnect behavior, stale-price fallbacks, and liquidity-monitor availability.
4. Confirm the emergency stop remains asserted and that no signed transaction or wallet submission occurs.
5. If the emergency stop blocks PAPER order simulation, record the run as **monitor-only soak**, not a full PAPER trade-flow soak. Do not disable the stop to force trades.
6. Export redacted logs and summarize uptime, restart count, error rates, data gaps, and all safety-block events. Remove API keys, wallet secrets, and personal identifiers before attaching logs.

## Acceptance criteria

- No LIVE mode and no signed transaction submissions.
- No unexplained process crash/restart loop.
- No duplicate execution attempts after ambiguous outcomes.
- Unresolved order/sell intents remain blocked until verified reconciliation.
- Data-provider outages are visible and never represented as verified-safe results.
- Liquidity alerts are reproducible from stored snapshots and remain alert-only.
- All exceptions and unresolved anomalies have an owner and disposition.

A successful CI run does not replace this runtime soak. The soak must be performed in the target deployment environment and independently reviewed before any LIVE consideration.
