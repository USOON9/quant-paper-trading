# 2026-09-07: Minute Data, Quotes, and Cost-Definition Validation

## Outcome

The read-only Alpaca historical market-data interface is connected, and the first batch of stock SIP and Alpaca US cryptocurrency data has been saved: 29,616 raw quotes and 3,272 raw minute bars, of which 3,267 fall within the specified sessions and pass the bar checks. All 18 request segments succeeded and completed pagination. This does not mean that every minute has data or that the model has passed trading admission checks.

This run did not submit orders, query accounts or positions, update models, or start continuous market-data collection. Both order gates remained closed. SHA-256 hashes of the existing v2 model and observation report, frozen v3 experiment files, research database, and existing automation configuration matched their pre-collection values.

Successful historical access does not establish real-time SIP entitlement. Alpaca distinguishes real-time and historical data permissions; this run tested only completed historical windows, not real-time streams, and purchased no subscriptions. [Official data-access documentation](https://docs.alpaca.markets/us/docs/about-market-data-api)

## 1. Actual Collection Scope

Capture directory: `artifacts/marketdata/20260907-sip-crypto-audit/`. The first observation was recorded at approximately 10:19 UTC on 2026-09-07; it must not be backdated as information known on the historical trading date.

- Stocks: SPY, JPM, XOM, WMT, and JNJ; the 13:30–20:00 UTC regular session on 2026-09-04; `sip` was explicitly requested.
- BTC/USD: the full UTC day on 2026-09-06; explicitly using `crypto/us`, which does not represent the global cryptocurrency market.
- Each symbol has full-session one-minute bars and two 60-second quote windows. Stock targets are 5 minutes after the open and 5 minutes before the close; BTC targets are 00:35 and 23:55 UTC.
- The selection rule was fixed before collection: use the first valid quote within 30 seconds after the target. No cost pair is produced if no valid quote exists or if distinct quotes at the earliest timestamp cannot be sequenced.

These are data-diagnostic windows, not predictions or labels from the frozen v3 experiment. Midpoint changes observed in this run must not be labeled model returns.

## 2. Minute Coverage and Spread Samples

| Symbol | Valid in-session minutes / Expected minutes | Missing minutes | Zero-volume bars | Approximate round-trip spread of the selected quote pair, bps |
| --- | ---: | ---: | ---: | ---: |
| SPY | 390 / 390 | 0 | 0 | 0.259 |
| JPM | 390 / 390 | 0 | 0 | 8.860 |
| XOM | 390 / 390 | 0 | 0 | 5.628 |
| WMT | 390 / 390 | 0 | 0 | 3.224 |
| JNJ | 388 / 390 | 2 | 0 | 13.021 |
| BTC/USD | 1,319 / 1,440 | 121 | 921 | 3.974 |

1 bps = 0.01%. The table reports half the entry full spread plus half the exit full spread, not the sum of two full spreads. Each full spread is calculated as `(ask − bid) / midpoint × 10000`. This describes quotes at only those two instants. It excludes actual account fees, slippage, queueing, market impact, and partial fills, and does not guarantee execution at the displayed prices.

Key findings:

- JNJ is missing the minute bars at 16:59 and 17:09 UTC; BTC is missing 121 bars. Missing data is explicitly recorded, with no zero filling, forward filling, or fabricated bars. The cause remains unverified and must not be attributed directly to network packet loss.
- The 921 zero-volume BTC bars are not evidence of executed trades. Alpaca documents that cryptocurrency bars may incorporate quote midpoints; when no trades occur, volume is zero and prices come from quotes. That is consistent with this observation, but it is not an event-by-event provenance verification for every bar. [Official cryptocurrency data definitions](https://docs.alpaca.markets/us/docs/real-time-crypto-pricing-data)
- The raw responses include 5 bars exactly at session-end boundaries. The API permits an inclusive end timestamp; analysis consistently uses `[start, end)`, retaining those raw records but excluding them from session coverage. [Historical stock bars API](https://docs.alpaca.markets/us/reference/stockbars), [Historical cryptocurrency bars API](https://docs.alpaca.markets/us/reference/cryptobars-1)
- Of the raw quotes, 12 failed basic validity checks and were excluded from statistics, while their raw files were retained. Complete exchange quote-condition, trading-status, and executability rules have not been implemented.
- The approximate spreads for these quote pairs in JPM, XOM, and JNJ already exceed 5 bps, indicating that a uniform low-cost assumption needs testing. Two instants on one day are insufficient to estimate a long-run cost distribution, so this run did not change v3 cost parameters or reselect a model.

The report also provides spread distributions across quote events within each window. These are event-weighted, not time-weighted: periods with faster quote updates receive more weight. They must not be interpreted as spreads averaged over holding time.

## 3. What Each Code Section Does

| Module | Responsibilities and boundaries |
| --- | --- |
| `src/quantpaper/marketdata/client.py` | Allows only historical bar/quote GET requests to the fixed `data.alpaca.markets` host. Bounds symbols, intervals, and page counts; rejects redirects; performs no automatic retries or silent SIP/IEX switching. Credentials are used only for request authentication and are never written to results or error text. |
| `src/quantpaper/marketdata/windows.py` | Uses the XNYS calendar to select completed stock sessions, handling holidays, daylight saving time, and early closes. Cryptocurrency data is divided into UTC days, with a 30-minute publication buffer in both cases. This is not a trading scheduler. |
| `src/quantpaper/marketdata/quality.py` | Checks timestamps, OHLC values, volume, missing minutes, duplicate/conflicting records, and abnormal quotes; preserves nanosecond precision; validates symbol, feed, request coverage, and observation time before calculating limited quote-cost diagnostics. |
| `src/quantpaper/marketdata/storage.py` | Writes results only to a new, separate capture directory; rejects existing directories and symbolic links. Create-only writes, disk synchronization, SHA-256 manifests, and read-time verification help prevent accidental overwriting of prior evidence. Hashes have no external signature and do not protect against replacement of all files together with their manifest. |
| `src/quantpaper/marketdata/cli.py` | Provides `capture` and `show`. Checks that both Paper gates are closed before collection; saves the plan, 18 raw segments, report, and completion manifest. It is isolated from model training, the shadow database, and order channels. |

By default, each segment is limited to 3 pages of up to 10,000 records each; reaching the limit must be marked incomplete. A permission failure does not trigger an automatic switch to IEX. SIP and IEX are not interchangeable sources, and Alpaca US cryptocurrency quotes are not a global consolidated quote. [Stock API feed and pagination definitions](https://docs.alpaca.markets/us/reference/stockbars)

Three statuses must be interpreted separately:

- `status=captured`: requests and pagination completed; this does not mean data quality or the strategy passed validation.
- Per-symbol `complete`: pagination completeness only; it can remain true when minutes are missing.
- `source_metadata_valid` in the latest analysis code: whether source fields and time coverage passed validation; invalid metadata blocks cost-pair output. The first frozen report was generated before this additional check was introduced and was not overwritten. All six symbols have been recomputed read-only with the latest code, passed the checks, and retained exactly the same original cost pairs and bar counts.

## 4. Running and Viewing Results

Use the existing `.venv` from the project directory, and do not send credentials in chat. Dependencies have been verified in the current environment; in `pyproject.toml`, the `data` optional dependencies now explicitly declare `requests`.

```bash
# Read and verify saved results: no network access, retraining, or orders.
.venv/bin/python main.py marketdata show --run-dir artifacts/marketdata/20260907-sip-crypto-audit

# Manually collect the latest completed windows; use a new directory name that does not exist.
.venv/bin/python main.py marketdata capture --run-dir artifacts/marketdata/NEW-CAPTURE-NAME --stock-feed sip

# Offline tests.
.venv/bin/python -m unittest discover -s tests -q
```

To reproduce the date range, explicitly add `--stock-session 2026-09-04 --crypto-session 2026-09-06`. Historical data returned by a later request may be revised, so it must be saved as a new observation version rather than overwriting the original sample.

The full project passed **175 tests** in this round, including 47 new tests for the client, safety isolation, trading windows, and data quality; dependency checks also passed. NumPy/pandas time-arithmetic deprecation warnings remain and require future compatibility work; this is not a zero-warning validation. Actual requests separately verified historical data access on this machine; offline mock tests do not replace production-interface validation.

## 5. Next Steps

The next stage should first freeze a read-only collection protocol spanning multiple trading days and intraday windows, estimate spread, missing-data, and quote-update distributions separately by symbol, and verify available actual fees and quote conditions. Execution replay should then incorporate latency and unfilled-order scenarios. Forward observations need actual arrival timestamps; historical data backfilled today cannot serve as features known in the past.

This round did not create a recurring collection task, validate actual options market data, or train a more complex model. The results support further research only, not enabling automated trading or claiming that the strategy is profitable.
