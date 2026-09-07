# As-of Quote Reconstruction and Offline Event Replay

## Purpose of this stage

Replace “find the first quote after the target time” with “use only the latest event before the target time.” The previous collection started at the target itself, so it could not establish the preceding state. This stage retains the same symbols, dates, and 90 target times, saves a complete short window extending 5 seconds before and 2 seconds after each target, and reconstructs quote state under rules fixed in advance.

“Before” refers only to the vendor's historical event timestamps. Historical data downloaded today do not contain the application's original local receive timestamps or prove that the bot actually received those quotes at the time. This is not a complete order book, a simulation of actual network latency, or a strategy-return backtest.

## Observed results

The supplementary capture completed during 2026-09-07 11:02:16–11:02:53 UTC: all 90 requests succeeded with complete pagination, saving **34,555 raw quotes**. The source study still covers stocks from 2026-08-31 through 09-04 and BTC from 2026-09-02 through 09-06. Sampling times were not changed in response to results.

- Capture and initial analysis: `artifacts/marketdata/20260907-asof-preroll/`.
- Independent, entirely offline recalculation: `artifacts/marketdata/20260907-asof-offline-check/`, with 0 HTTP requests.
- Every state, reference price, rejection reason, and aggregate result matched across all 90 windows in both runs. Input files and the source snapshots remained unchanged during execution.

### Quote state at the decision time

| Symbol | Target times | Passed quote checks | No preceding event | Preceding quote stale |
| --- | ---: | ---: | ---: | ---: |
| SPY | 15 | 15 | 0 | 0 |
| JPM | 15 | 13 | 0 | 2 |
| XOM | 15 | 14 | 0 | 1 |
| WMT | 15 | 14 | 0 | 1 |
| JNJ | 15 | 8 | 3 | 4 |
| BTC/USD | 15 | 2 | 7 | 6 |
| Total | 90 | 66 | 10 | 14 |

All source metadata were usable in this run. None of the selected decision times fell in an invalid state, but invalid updates and timestamp ambiguities did occur within the windows. A usable state at a particular decision does not justify ignoring invalidation elsewhere in the event stream.

The three offset scenarios produced 270 checks: 193 passed reference-quote checks only; 72 remained rejected because the original decision was already blocked (24 target times × 3 scenarios); another 5 changed from a valid decision state to an invalid or stale arrival state. Of those, two involved distinct updates with the same timestamp and no determinable ordering, and three exceeded the quote-age limit. The 0, 250, and 1,000 millisecond scenarios had 66, 65, and 62 target times passing quote checks, respectively.

One observed example: near the XOM close on September 1, the latest quote at the decision time was approximately 896 milliseconds old and passed the checks. At the 250 millisecond offset, the latest timestamp had two different updates without usable sequence numbers, so the state became `INVALID`. Replay did not skip that update and reuse an older price. JNJ near the open on September 4 illustrates a different case: the original quote was approximately 7.604 milliseconds old; after the 1 second offset, its age was 1,007.604 milliseconds, making it `STALE` under the fixed rule.

BTC's 2/15 is not a fill rate and cannot support a conclusion about global BTC liquidity. It describes only these target times, the Alpaca US source, the 5 second warmup window, and the 1 second age rule. This differs from the previous method, which allowed waiting up to 30 seconds for a future quote, and does not indicate a decline in model performance.

The full project passed **293 tests** (62 added in this stage), along with compilation and dependency checks. Existing NumPy/pandas datetime-operation deprecation warnings still require future compatibility work. Hashes of the prior v2/v3 model evidence, shadow report, database, previous capture completion indexes, and existing automation configuration remained unchanged. Both order gates stayed closed; actual order submissions and simulated-fill ledger entries were both 0.

## Fixed rules and rejection reasons

| State | Meaning | Handling |
| --- | --- | --- |
| `VALID` | The latest event is strictly before the check time, no more than 1 second old, and passes quote filters | Record buy/sell reference prices only; do not record fills |
| `MISSING` | No event in the window is strictly before the check time | Do not backfill with a future quote |
| `STALE` | The latest valid quote is more than 1 second old | Do not reuse the stale price |
| `INVALID` | The latest update contains zero/negative/nonfinite prices or sizes, a crossed/locked quote, disallowed conditions, or timestamp ambiguity | Invalidate state; do not fall back to an earlier valid price |
| `SOURCE_REJECTED` | Source, range, pagination, timing, or other metadata are unreliable | Exclude the entire window from reference-price use |

Specific constraints:

- Apply strict `quote.t < asof`. Equal timestamps do not establish ordering, so they cannot establish that a quote was already known at that time.
- The latest event governs state. Quote clearing, invalid updates, and distinct updates sharing a timestamp can invalidate it; only a strictly later valid update can restore it. Exactly identical duplicates are deduplicated without creating ambiguity.
- A quote age of exactly 1,000 milliseconds is allowed; anything older is stale. This is a fixed research assumption, not a calibrated execution parameter, and must not be relaxed after observing the availability rate.
- Stocks accept only `c=['R']` and known tape A/B/C, and reject locked/crossed quotes and nonpositive prices or sizes. BTC uses the separate `crypto_us` convention without stock condition codes.
- `R` refers only to the quote-condition namespace here; trade-condition codes must not be mixed into it. A regular quote condition alone does not establish the absence of a halt, automated executability, or a fill at the displayed size. Those require additional market-status information. [Alpaca field definitions](https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data), [UTP 2026 quote specification](https://www.utpplan.com/DOC/UtpBinaryOutputSpec.pdf)
- The API may return records at its end boundary. Internal analysis consistently filters to `[start, end)`; the API convention and the internal analysis convention are distinct.
- Incomplete requests, more than 3 pages, mismatched sources, or unparseable event timestamps must not silently become complete, orderable historical windows.

## Replay arrival state without inventing fills

For the same decision time, inspect state after event-time offsets of 0, 250, and 1,000 milliseconds. Only when both the decision and offset states are valid does the result become `QUOTE_ELIGIBLE_NO_FILL_ASSUMED`, with the ask recorded as a buy reference price and the bid as a sell reference price.

A blocked decision remains `DECISION_BLOCKED` even if a future quote restores valid state. Replay must not retrospectively create a trade that should never have occurred. A valid decision followed by an unusable arrival state is `ARRIVAL_BLOCKED`. These scenarios are not independent orders, and the report counts are not fill rates.

This stage calculates no order quantities, account-balance changes, positions, fees, or PnL. Even Alpaca's own Paper fills omit aspects of market impact, queueing, and latency, so reference quotes cannot be treated as actual trading fills. [Alpaca Paper simulation limitations](https://docs.alpaca.markets/us/docs/paper-trading)

## New code sections

| Module | Responsibility |
| --- | --- |
| `replay_protocol.py` | Reconstruct the same symbols, dates, and targets from the verified five-day study plan; freeze short windows, ordering, age, and condition rules; prohibit outcome-dependent resampling. |
| `replay_book.py` | Update quote state in event-time order, handling invalidation, recovery, staleness, and missing data; reconstruct decision and offset states; expose no network, account, or order interface. |
| `replay_runner.py` | Verify input-plan, source-code, and raw-data hashes; perform a one-off read-only supplementary capture or entirely offline recalculation; save a fresh output directory and audit index without overwriting earlier evidence. |
| `marketdata/cli.py` | Add `replay-capture` and `replay` commands while retaining offline `show`; never route to trading execution. |

## Usage

Use the existing `.venv` from the project directory. Every output directory must be new:

```bash
# Fetch short quote windows for the same targets and analyze them; do not submit orders.
.venv/bin/python main.py marketdata replay-capture --source-dir artifacts/marketdata/20260907-five-session-study --run-dir artifacts/marketdata/NEW-ASOF-CAPTURE

# Recalculate from the previous step's frozen raw data entirely offline, without API calls.
.venv/bin/python main.py marketdata replay --source-dir artifacts/marketdata/NEW-ASOF-CAPTURE --run-dir artifacts/marketdata/NEW-OFFLINE-REPLAY

# Verify and display the results.
.venv/bin/python main.py marketdata show --run-dir artifacts/marketdata/20260907-asof-offline-check
```

The capture client still sends only GET requests to the fixed market-data host, with at least 0.4 seconds between page requests. It does not automatically retry or switch sources. A request failure stops subsequent requests while retaining uncollected windows and the sample denominator. Offline replay does not construct a market-data client or require account API calls.

Hashes detect mismatches between files and their index, but are not external signatures. Malicious replacement of the entire file set and index together is outside the current guarantee. A source snapshot is not a complete executable environment image.

## Unresolved trading requirements

Historical receive timestamps, reliable halt/resumption streams, a complete quote-condition matrix, order-book queueing, actual tradable size, real fees, execution reports, and position reconciliation remain unavailable. Passing this stage's research quote filters does not establish that these requirements are satisfied. No ML model was updated, no automation was added, and no trading gate was opened.

The next stage should connect market-state checks to simulated order-intent admission checks and lifecycle logs, with explicit rejection reasons for missing quotes, stale data, unknown state, or an unapproved model. Actual fills still require independent matching assumptions and account reconciliation. This stage's reference prices must not be converted directly into profit records.
