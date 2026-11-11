# Security and LIVE trading readiness

## Current operating rule

Keep `TRADING_MODE=PAPER`. The presence of `live_trader.py` and
`jupiter_executor.py` means LIVE execution code exists; it does **not** mean
that real-money execution has been audited or validated. Do not enable LIVE
on a funded wallet based only on a successful import or CI compile.

## Release blockers before LIVE

The following items require implementation and/or evidence from controlled
tests before LIVE can be considered:

1. **Transaction policy validation** — validate input/output mints, positive
   input amount, quote response fields, minimum output, maximum price impact,
   and allowed slippage immediately before signing. Reject malformed or
   out-of-policy quotes.
2. **Transaction signer and instruction inspection** — verify the fee payer,
   required signer set, recent blockhash, and permitted instructions in the
   deserialized Jupiter transaction before signing. Never sign an arbitrary
   transaction returned by an external API without inspecting it.
3. **Wallet and balance limits** — enforce per-trade, per-token, daily-loss,
   and total-exposure caps in the execution layer (not only in Telegram/UI
   settings). Fail closed when balance or price data is unavailable.
4. **Token amount precision** — do not silently assume six decimals when
   `getTokenSupply` fails. A wrong decimal count can cause a materially
   incorrect sell amount. Abort the trade when decimals cannot be verified.
5. **Confirmation and reconciliation** — distinguish a send timeout from a
   definitive transaction failure. Before retrying a swap, query the original
   signature and reconcile wallet token/SOL balances to prevent duplicate
   economic actions.
6. **Multi-wallet accounting** — reconcile each partial MultiBuy fill and
   each wallet's holdings independently. A combined position must not imply
   that one wallet can sell tokens held by another.
7. **Secret handling** — ensure API keys and private keys never enter logs,
   Telegram messages, exceptions, test fixtures, or committed files. Rotate
   any secret that has ever been committed.
8. **Operational kill switch** — provide a server-side emergency stop that
   blocks new buys and remains effective even if Telegram is unavailable.

## Existing safeguards observed in the code

- Configuration defaults to PAPER mode.
- The wallet loader avoids including the private key in its validation error.
- The executor signs locally; the private key is not intentionally sent to
  Jupiter.
- LIVE position accounting is updated only after the executor reports a
  confirmed swap.
- CI currently compiles Python sources and checks the PAPER default.

These safeguards are useful but do not satisfy the release blockers above.

## Required validation sequence

1. Run static compilation and the full automated test suite.
2. Exercise quote parsing and transaction inspection with mocked responses,
   including malformed and adversarial payloads.
3. Run end-to-end PAPER tests for buys, partial sells, full sells, retries,
   restarts, and MultiBuy partial failures.
4. Run devnet/integration tests with a disposable wallet and negligible funds
   where supported; verify actual token decimals and balance reconciliation.
5. Run a sustained PAPER trial and compare recorded fills with independent
   on-chain data.
6. Only after independent review, enable LIVE with a dedicated low-balance
   wallet and strict limits. Never use a wallet holding long-term assets.

## Incident response

If unexpected signing, spending, or position accounting occurs: stop the bot,
disable LIVE, revoke/rotate exposed API credentials, move remaining assets to
a clean wallet if the signing key may be compromised, and preserve logs and
transaction signatures for investigation.


## Controls added in the audit update

The executor now rejects malformed quotes, mismatched mints and amounts, slippage or price impact above local limits, unexpected fee payers/signers/programs, non-Jupiter transactions, invalid blockhashes, excessive estimated fees, and direct top-level SOL/SPL instructions outside its narrow swap policy. The Telegram interface requires a private user ID in `TELEGRAM_ALLOWED_USER_IDS`; an empty list denies access.

These checks do not close the blockers above. In particular, transaction timeout reconciliation before retry, execution-layer exposure limits, verified token decimals, per-wallet MultiBuy accounting, and a server-side kill switch still need implementation and controlled evidence. Keep LIVE disabled.
