# Supervised Alpaca Paper engineering pilot

## Scope

This stage provides a bounded, supervised **engineering** round trip, not an
autonomous trading strategy. It buys one small SPY notional and then attempts to
sell exactly the confirmed filled quantity. It does not load a model, use the
new research features, change model approval, hold a strategy position overnight,
trade options or crypto, or implement HFT.

The first pilot supports SPY only. That is a narrow test boundary, not an
investment recommendation. The default is $5 of simulated notional; the hard
range is $1–$25 in whole cents. Use a dedicated Paper account with no other
trading clients or manual activity during the supervised test.

This code does not enable either existing execution gate. A real Alpaca Paper
attempt requires a separate user-approved symbol, amount, run ID, and timing,
plus both exact opt-in gates. Never copy live credentials into this workflow.

## Safe commands first

```bash
# Entirely synthetic: no credentials, SDK client construction, or broker calls.
python main.py alpaca rehearse

# GET-only account/asset/clock/quote inspection with local Paper credentials.
# Does not reserve a daily attempt or authorize an order.
python main.py alpaca preflight --symbol SPY --notional 5

# Other read-only commands remain available.
python main.py alpaca status
python main.py alpaca quote --symbol SPY
```

`preflight` returns 0 for `PASS`, 3 for `BLOCKED`, and 2 for a command or transport
failure. A successful preflight is advisory, not an execution token. Its output
contains sanitized reason codes, timing diagnostics and hashes, not account IDs,
balances, positions, raw quotes, or credentials. `status` remains an intentional
account-summary command and displays balances/positions to its local caller.
Errors and invalid CLI arguments do not echo provider payloads or supplied
argument values. Do not supply credentials as CLI arguments.
The normalized snapshot is not retained by this read-only command: its hash is
an integrity reference, not an independently replayable archive or an
authenticated broker signature.

The obsolete `submit-cancel` entry point is disabled both in the CLI and service:
even a very low limit order can fill, and that separate path must not evade the
new pilot checks. Use the synthetic cancellation scenarios or the guarded pilot.

## Frozen entry checks

| Check | Required condition |
| --- | --- |
| Account | ACTIVE, USD, no account/trading/user-suspension blocks |
| Whole-account inventory | No positions, including other symbols; no open orders |
| Symbol | SPY, active US equity, tradable and fractionable |
| Notional | $1–$25, whole cents; default $5 |
| Cash | At least notional plus a $1 engineering reserve |
| Buying power | At least the requested notional; no leverage is requested |
| Equity | Current equity and previous-close equity both positive |
| Account loss proxy | Block if previous-close equity minus current equity is at least $5 |
| Session | Broker says regular market open; strictly more than 5 minutes remain before close |
| Clock / capture | Broker-clock skew at most 5 seconds; sequential collection at most 10 seconds |
| Quote | IEX, correct symbol, no future timestamp, age at most 2 seconds |
| Quote values | Finite positive bid, ask and displayed sizes; ask not below bid |
| Relative spread | `(ask-bid)/mid * 10000` at most 20 bps |
| Local state | No unresolved incident, reused run ID, concurrent local operation, or earlier attempt on the same New York date |

The equity-change check is a conservative **account-level proxy**, not a
cash-flow-adjusted trading-loss ledger. Deposits and withdrawals can affect it.
The $1 reserve is not an estimate of fees. Quote and timing limits are frozen
engineering assumptions, not calibrated profitability or fill guarantees. IEX
is not a verified consolidated NBBO. Fractional eligibility is checked because
not every asset supports fractional orders; see the
[Alpaca fractional-trading specification](https://docs.alpaca.markets/us/docs/fractional-trading).

All SDK datetimes are normalized to UTC without assigning a timezone to naive
values. Malformed or missing fields block entry. A single nonempty position or
open-order list is enough to block, so a bounded open-order query need not prove
the exact total count to reject the pilot.

## Order lifecycle and recovery

1. Validate the explicit request and both execution gates before broker reads.
   Acquire the existing per-key lock and check old incident markers.
2. Fetch fresh account/asset/clock/position/order/quote information and evaluate
   the entry policy. Acquire the shared **account-scoped** lock and durably
   reserve this run before any order submission.
3. Write the existing lifecycle/intent audit. Re-evaluate snapshot freshness
   after the audit write, immediately before the entry POST. If the process
   paused and the quote expired, do not submit.
4. Submit one market-notional entry with a deterministic client order ID.
   Bind every accepted/polled/recovered response to its order ID, client ID,
   symbol, side and order type. Reject invalid quantities/prices or a cumulative
   filled quantity that decreases across submission, polling or cancellation.
5. Confirm the entry is terminal. A partially filled then cancelled entry still
   has exposure: submit a market exit for exactly its confirmed filled quantity.
   Entry freshness, cash and permission gates do not veto this risk-reducing exit.
6. Confirm exit quantity matches entry quantity, then fetch the same account
   again and require the entire account to have no positions or open orders.
   Only then acknowledge completion and close the durable reservation.

Cancellation acceptance is not terminal confirmation. A transport error can
follow successful order acceptance: query the **same** client order ID rather
than posting again. SDK automatic retries are disabled, including retries on
429 responses. If acceptance, cancellation, fills, identity, storage or final
account state is uncertain, retain a reconciliation block and stop. There is
no automatic resume, position liquidation, block deletion, or recovery command.

Two durable controls complement one another:

- `data/paper-state/`: existing API-key incident markers and legacy-marker checks.
- `data/paper-pilot/`: account-hash-bound reservation ledger and account lock.
  Different API keys and audit paths for the same account share this guard on
  this host. Completed run IDs are never reusable. At most one reservation is
  allowed per `America/New_York` calendar date, across all runs. An unresolved
  reservation blocks later dates too. Clock rollback and a full 1,000-run ledger
  require intervention; records are not automatically pruned.

Reservations use exclusive locks, no-symlink directory traversal, checksummed
state, atomic replacement and file/directory synchronization. A reservation is
durable before control reaches order submission. Calling the completion method
is insufficient by itself: the surrounding operation must also exit normally.
Completion-write failures attempt to retain or invalidate the unresolved state.
An actually failing disk cannot guarantee a new durable write; detected I/O
errors always stop the operation.

Never delete a guard file to make a retry pass. First reconcile the original
client IDs, broker orders, fills and positions, and retain the evidence. A
maintenance/recovery protocol is still required before broader unattended use.

## Transport boundaries

The service constructs the official SDK with `paper=True`. Its session permits
only `https://paper-api.alpaca.markets/v2/` for trading and
`https://data.alpaca.markets/v2/` for data. There is no live URL override.
Read-only service instances reject mutations both at the public round-trip
method and at the HTTP layer; data transport always permits GET only.

Requests use fixed 3.05-second connect and 5-second read timeouts, no redirects,
no SDK automatic retry, and no implicit proxy or `.netrc` authentication.
These are socket/request controls, not a hard end-to-end latency guarantee.
Original-client-ID lookup handles uncertain submissions. The installed SDK's
session/retry integration is covered by offline tests and must be revalidated
when upgrading dependencies.

## A separately authorized Paper attempt

This is documentation, not a request to enable or run trading. After the user
approves one supervised attempt, both gates must be explicitly configured:

```dotenv
ENABLE_ALPACA_PAPER=YES_I_UNDERSTAND
ENABLE_ALPACA_PAPER_ROUND_TRIP=YES_RUN_SMALL_ROUND_TRIP
```

Then a single bounded invocation uses an explicit new identifier:

```bash
python main.py alpaca round-trip --symbol SPY --notional 5 --run-id supervised-spy-001
```

There is no loop, cron task, model callback, or automatic repeat. Return both
gates to `NO` after the separately supervised acceptance procedure. Existing
shadow runs require closed gates, so do not overlap a manual pilot with the
scheduled shadow process or change the schedule implicitly.

## What each section does

| Code | Responsibility |
| --- | --- |
| `paper_policy.py` | Pure normalized-data preflight, strict numeric/time checks, frozen policy hash and sanitized reasons |
| `paper_guard.py` | Account-scoped daily budget, durable reservations, deterministic client IDs and unresolved-state blocking |
| `paper_transport.py` | Fixed Paper/data hosts, GET-only read-only mode, bounded request timeouts and disabled retries |
| `alpaca_paper.py` | Existing SDK service hardened with preflight, guard, bound broker evidence and final whole-account checks |
| `paper_rehearsal.py` | Six deterministic synthetic scenarios through the same production lifecycle, using temporary state only |
| `alpaca_cli.py` | Safe command dispatch; rehearsal avoids credential loading and preflight cannot submit |
| `tests/test_paper_*.py` / `tests/test_alpaca_lifecycle.py` | Policy boundaries, transport isolation, durable failure behavior and lifecycle regressions |

## Still not an autonomous strategy

The pilot uses REST polling and requires a human available for uncertain states.
It does not provide trade-update streaming, cross-host/account coordination,
automated restart recovery, a full broker cash/fee ledger, a strategy holding
period, position sizing from ML, or a validated executable backtest. External
manual trades can race sequential snapshots; local checks do not make broker
reads atomic. Deleting or coordinately rewriting local state defeats local-file
history protections without an external trusted anchor.

Passing rehearsal proves only the tested synthetic behavior. It is not evidence
of a successful live connection, an Alpaca Paper fill, an approved model, or a
profitable strategy. Alpaca Paper itself does not fully simulate market impact,
latency slippage or queue position, as described in its
[official Paper documentation](https://docs.alpaca.markets/us/docs/paper-trading).

## Verification on 2026-09-08

- All 748 project tests passed locally, including 113 newly added tests; the
  existing 11 lifecycle regressions were updated without dropping their checks.
- The executable rehearsal passed all six scenarios: whole fill, partial entry
  with confirmed cancellation, duplicate/same-account retries, stale quote,
  unknown submission, and residual exit. Its 9 submission calls were synthetic;
  actual broker requests and Alpaca orders from rehearsal were both zero.
- A separately invoked GET-only preflight at 20:43:11 UTC successfully queried
  Alpaca but returned `BLOCKED`: `MARKET_NOT_OPEN`, `QUOTE_STALE`, and
  `QUOTE_NUMERIC_INVALID`. The quote timestamp was 20:00:02 UTC. These failures
  were retained, not relaxed to obtain a pass. No daily attempt was reserved.
- Both effective execution gates remained closed. No orders were submitted,
  cancelled or modified, and no models or schedules were changed.
- Byte hashes for all 437 protected files matched the pre-work baseline,
  including `.env`, the warehouse, model inputs/models, and existing artifacts.
  Publication candidate scanning found no secret issues; credentials, broker
  state and audit artifacts remain Git-ignored.

The next operational step is a fresh preflight during an eligible regular
session, followed by separate user confirmation for a single supervised Paper
attempt. This stage did not schedule that attempt or demonstrate an actual
Alpaca fill.
