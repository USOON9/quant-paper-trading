# Multi-Day, Multi-Window Market-Data Validation: 2026-09-07

## Purpose of This Stage

The previous round had quotes from only one day and two instants per symbol, which cannot establish long-run execution costs. This study fixes the sampling protocol in advance and expands coverage to five complete sessions per symbol, with three quote windows per day. Its purpose is to inspect data gaps, day-to-day and intraday spread differences, and sensitivity to quote-selection time, not to train a new model or generate strategy returns.

## Actual Results

A single collection run completed on 2026-09-07 at 10:38:52–10:39:48 UTC. Results are saved in `artifacts/marketdata/20260907-five-session-study/`.

- Stock dates span 2026-08-31 through 2026-09-04; BTC dates span 2026-09-02 through 2026-09-06. There are 5 days per symbol, totaling 30 symbol-days and 90 quote windows.
- All 120 request segments succeeded, requiring 128 HTTP requests/response pages in total. There were no errors, stopped requests, or pagination truncations. Successful historical access still does not establish real-time entitlement.
- Saved data comprises 206,663 raw quotes and 16,603 raw minute bars. Analysis found 16,577 valid in-session bars; another 26 session-end boundary bars remain in the raw records but are excluded from their sessions.
- Source metadata passed validation for all 30 symbol-days. Of the 90 quote windows, 88 had a quote eligible under the selection rule, and 89 had a usable complete-window distribution. Statistics exclude 89 invalid quotes while retaining the raw records.

### Observed Coverage and Spreads by Symbol

| Symbol | Valid / Expected minutes | Missing minutes | Zero-volume bars | Median daily approximate round-trip spread of opening/closing quote pairs, bps |
| --- | ---: | ---: | ---: | ---: |
| SPY | 1,950 / 1,950 | 0 | 0 | 0.261 |
| JPM | 1,950 / 1,950 | 0 | 0 | 8.860 |
| XOM | 1,950 / 1,950 | 0 | 0 | 5.628 |
| WMT | 1,950 / 1,950 | 0 | 0 | 3.224 |
| JNJ | 1,946 / 1,950 | 4 | 0 | 9.766 |
| BTC/USD | 6,831 / 7,200 | 369 | 4,248 | 3.429 |

Each symbol has 5 daily paired samples in the spread column. The entry half-spread plus exit half-spread is calculated for each day, then the median is taken across days. 1 bps = 0.01%. These figures exclude fees and slippage and provide no execution guarantee. They are not total costs and must not be used to change the frozen v3 experiment's cost configuration: the diagnostic entry times differ from v3 daily labels.

Intraday differences are also substantial. For example, taking each day's event-weighted median full spread within a window, then the median of those daily values, gives JNJ spreads of 13.545 bps near the open, 4.009 bps at midday, and 2.542 bps near the close. The corresponding JPM values are 10.081, 2.527, and 1.973 bps. This supports continued symbol-specific and time-window-specific modeling, not applying one uniformly low spread assumption to every trade.

### Issues That Must Not Be Ignored

BTC minute coverage is 94.875%, not a complete historical trade tape. September 5 and 6 are missing 210 and 121 minute bars, respectively. The September 5 midday window contains 12 raw quotes but no valid quote within 30 seconds after the target; the same window on September 6 returned zero quotes. Both days remain in the denominator: no quote is not treated as zero spread. The other 3 BTC midday windows have selectable quotes, so the selected-quote sample count for that window is 3/5, not 5/5.

The 4,248 zero-volume BTC bars are not evidence of executed trades. Alpaca states that its cryptocurrency bars may incorporate quote midpoints; when no trades occur, volume is zero and prices come from quotes. Bars from this source therefore do not have the same definition as pure trade aggregates. [Official cryptocurrency bar definitions](https://docs.alpaca.markets/us/docs/real-time-crypto-pricing-data)

Timing sensitivity matters, but this run does not present it as a fill simulation. For example, near the WMT close on September 3, the selected quotes in the zero-delay and 1-second-shift scenarios are approximately 991 milliseconds apart in actual event time, with the bid falling approximately 18.401 bps. This is one historical price change in that window, not expected slippage, a loss, or a tradable signal. Conversely, some BTC scenarios select the same later update and show zero price change; that does not mean latency is risk-free. The full report retains waiting times and actual quote-event gaps for verification.

The full project passed **231 tests** (56 added in this round), along with compilation and dependency checks. NumPy/pandas time-arithmetic deprecation warnings remain; dependencies were not upgraded in this round. Hashes of the existing v2/v3 model evidence, observation report, research database, previous capture's completion manifest, and existing automation configuration matched before and after collection; both order gates remained closed. Snapshots of 13 source files were unchanged during collection.

## Frozen Protocol

- The stock universe remains SPY, JPM, XOM, WMT, and JNJ, using historical SIP data. The XNYS calendar selects the latest five completed sessions after a 30-minute buffer.
- BTC/USD is sampled separately over the latest five complete UTC days using Alpaca US venue data; its statistics are not pooled with stocks.
- Each symbol-day includes full-session one-minute bars and three 60-second quote windows: opening, midday, and closing. Stock targets are 5 minutes after the open, the session midpoint, and 5 minutes before the close; BTC targets are 00:35, 12:00, and 23:55 UTC.
- Defaults are 30 symbol-days and 120 request segments, with a maximum of 3 pages per segment. The date range is not a random sample and does not guarantee coverage of different market regimes.
- The plan and source-code snapshot are saved before the first market-data request. Raw data, the analysis report, and the SHA-256 manifest are written to a new directory without overwriting previous captures, models, or databases.

The client enforces at least 0.4 seconds between HTTP requests, including pagination. This rate limit applies only to the current client and does not ensure that other programs are not consuming the same account allowance. An HTTP error stops subsequent requests, with no automatic retry or source switching. Alpaca's current official documentation distinguishes historical request allowances from real-time data coverage; a successful historical SIP request does not validate real-time SIP entitlement. [Official data permissions and limits](https://docs.alpaca.markets/us/docs/about-market-data-api)

## Interpreting the Statistics

Statistics are computed separately for each symbol and window, with equal weight for each day. An event-weighted median spread is first calculated within each window, then those daily medians are summarized. Quotes from all five days are not pooled directly, and the six symbols are not combined into one cost estimate.

A selected quote is the first valid update within at most 30 seconds after the target whose timestamp has no sequencing ambiguity. It is not a snapshot of the quote in effect at the target and does not guarantee execution. Incomplete quotes, inconsistent sources, no valid quotes, same-timestamp conflicts, and stopped requests all remain in the sample denominator, with unavailability reasons reported separately.

The 250-millisecond and 1-second delay scenarios shift only the selection starting point in historical event time. A valid update may still take up to another 30 seconds to appear, so these scenarios are not measured network latency, exact-time BBO snapshots, or order-execution replay. Price-change comparisons use only paired zero-delay and delayed quotes available within the same day and window, and report sample counts. Differences between separate sample groups are not substituted for paired changes.

No account fees, net returns, fill probabilities, capacity, or strategy win rates are calculated. Quote conditions, trading status, order-book updates and invalidation, actual receipt timestamps, order acknowledgments, queueing, market impact, and partial fills still require independent verification.

## New Code Sections

| Section | Purpose |
| --- | --- |
| `study_protocol.py` | Determines completed sessions, the three windows, symbols, sources, and request budget; handles holidays, daylight saving time, and early closes. |
| `study.py` | Checks that order gates are closed; freezes the plan and code; collects data read-only; stops on errors; saves the full sample denominator, raw data, and report. |
| `study_analytics.py` | Checks data and quotes by day, calculates spreads and event-time sensitivity, and aggregates with equal daily weight by symbol/window; has no order or model connection. |
| Rate-limit extension in `client.py` | Uses a monotonic clock to rate-limit each GET page request and records attempt counts; retains the fixed host, redirect prohibition, and zero automatic retries. |

## Usage

Use the existing Python environment from the project directory:

```bash
# One-time, read-only collection; the directory name must be new.
.venv/bin/python main.py marketdata study --sessions 5 --stock-feed sip --run-dir artifacts/marketdata/NEW-STUDY-NAME

# Verify and view this run's actual results offline, without collecting again.
.venv/bin/python main.py marketdata show --run-dir artifacts/marketdata/20260907-five-session-study

# Offline regression tests.
.venv/bin/python -m unittest discover -s tests -q
```

The program limits each asset to at most 10 completed sessions. `captured` means only that requests and pagination completed; quality, metadata, and coverage require separate inspection. Existing directories cannot be overwritten by rerunning a capture. Partial files left by an interrupted collection must not be treated as completed evidence.

This remains a one-time research sample. It did not add a scheduled task, start a continuous market-data stream, or authorize automated trading.

## Boundaries for the Next Stage

Next, reconstruct the quote in effect at the target and define stale/unavailable-state rules, distinguishing the last known quote from the first update after the target. Then implement event replay without guaranteed fills, explicitly recording missing quotes, stale quotes, and refused simulated fills. Until actual account fees, trading conditions, and position reconciliation have been validated, do not claim actual net returns or enable automated trading.
