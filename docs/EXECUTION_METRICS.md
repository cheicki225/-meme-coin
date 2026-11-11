# Execution performance telemetry

This module adds local, privacy-conscious timing measurements without changing
transaction construction, signing, submission, retry, or confirmation behavior.

## Usage

```python
from execution_metrics import measure_execution

with measure_execution("jupiter_quote", provider="jupiter"):
    quote = await get_quote(...)
```

For an ambiguous network result, set the yielded state to `unknown` before
raising. Never retry a transaction merely because its confirmation is delayed.

Events are appended as JSON Lines to `data/execution_metrics.jsonl` by default.
Set `EXECUTION_METRICS_PATH` to choose another path. Keep this file private:
although the recorder is designed to store no wallet addresses, signatures,
keys, balances, token mints, or raw API responses, operational telemetry should
still be treated as internal data.

Use `summarize_metrics(path)` to obtain counts, failure rates, and p50/p95/p99
latency by stage and provider. Compare providers only with equivalent request
types and time windows. These measurements do not guarantee transaction
inclusion or profitability.

Telemetry is best-effort and fails closed from an observability perspective:
write errors are ignored and never affect execution. Trading safety controls
remain authoritative and independent of this module.
