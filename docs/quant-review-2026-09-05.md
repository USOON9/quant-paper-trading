# Quant Code Review and Remediation Record

Review date: 2026-09-05. Scope: research data, ML validation, shadow signals, synthetic matching, and the Alpaca Paper order lifecycle in the local Python project. No order submission or cancellation endpoints were called, neither trading gate was changed, and no credentials were read or displayed for this report.

## Conclusion

The project is a prototype suitable for further research, not a strategy with a demonstrated trading edge or a production HFT system. This review prioritized return-accounting correctness, whether information was available at the relevant time, and whether orders could leave uncontrolled exposure. The existing gradient-boosted tree remains an interpretable, reproducible baseline; no larger or newer model was connected directly to trading.

The complete suite passed **88 tests**; `pip check` found no dependency conflicts, and source compilation checks passed. This establishes that the covered engineering behaviors passed their tests, not that the strategy is profitable or every failure mode is covered. Date arithmetic with NumPy 2.5 / pandas 2.3 still emits deprecation warnings. Upgrades should be validated in a separate environment; future compatibility should not be ignored.

## Issues Fixed in This Review

P1 denotes issues that could compromise trading state or research conclusions and should be addressed before research continues. P2 denotes important reliability and reproducibility issues.

| Priority | Original issue and impact | Remediation | Main locations |
| --- | --- | --- | --- |
| P1 | `partially_filled` could be treated as `filled`, a cancellation ACK as a terminal state, and timeouts could leave unknown exposure | Use exact enumerated states; confirm entry termination before handling the actual filled quantity; query uncertain submissions using the original client ID; persistently block residual or unknown states | `alpaca_paper.py`, `tests/test_alpaca_lifecycle.py` |
| P1 | Changing the audit path could bypass the previous incident block | Scope the lock and fixed project state directory by API key hash; retain compatibility with legacy markers; changing the log path with the same key does not bypass the block | `data/paper-state/`, `alpaca_paper.py` |
| P1 | Unfilled orders did not fully reserve risk; another instrument's quote could trigger a fill against stale prices; replay completion could manufacture fills | Reserve pending-order risk by direction; use only the instrument's post-arrival quotes; share visible liquidity; constrain slippage by limit prices; cancel unfinished orders at EOF | `risk.py`, `execution.py`, `engine.py` |
| P1 | Portfolio averaging implicitly changed capital allocations when stock and crypto trading dates differed | Fix equal capital allocations by instrument; hold missing/FLAT allocations in cash; annualize over calendar days; include first-day losses in maximum drawdown; specify full round-trip costs | `ml/metrics.py` |
| P1 | Cross-market daily feature joins and training/inference were inconsistent; old rows could be reused silently | Use backward time joins with conservative availability dates and expiry limits; reject invalid latest features; fix RSI for uninterrupted price increases; add future-data perturbation tests | `ml/features.py`, `ml/regime.py` |
| P1 | Shadow targets were not fixed in advance; backfilling the first available bar could change the original prediction problem | Fix the target session/open/close before writing; reject late or stale features; settle only that target and leave missing outcomes pending; retain complete features and settlement OHLC/hash | `shadow.py`, `sessions.py` |
| P1 | By the time the complete previous-day BTC bar was published, the next UTC day's open had already passed | Pause BTC daily shadow; do not assume execution at a price from the past; wait for separately defined delayed-entry labels | `shadow.py`, `scheduler.py` |
| P1 | SEC quarterly/cumulative periods or different units could overwrite each other; FRED versions/pagination were incomplete; Yahoo backfill was treated as historically available | Retain reporting-period start/end, units, and versions; validate FRED real-time intervals and pagination; assign conservative local-time availability; version Yahoo data by actual first observation | `sources/`, `warehouse.py` |
| P2 | Old models could be mixed with new features; default automatic early stopping used non-temporal validation; single-class test folds were skipped | Pair model SHA-256 with metadata and the feature contract; freeze the iteration count and disable internal automatic early stopping; retain single-class folds with null AUC | `ml/training.py`, `ml/walkforward.py` |
| P2 | Shadow records could duplicate across dates, writes could be partial, and locks were acquired too late; audit validation checked only the chain tail | Use a unique target-session key; lock before opening the DB; settle and insert within one transaction; replace reports atomically; verify the complete audit chain with file locking and fsync | `shadow_cli.py`, `audit.py` |
| P2 | The calendar's default historical range could discard early data; optional dependencies were incompletely declared | Request calendars for the input date range and test 1993 data; add data dependencies; record a snapshot of tested versions | `sessions.py`, `pyproject.toml`, `requirements-tested.txt` |

The calendar handles early closes, holidays, and DST. The 30-minute post-close buffer is a conservative project assumption, not a guarantee that vendor data is final. The database and JSON files do not share a cross-resource transaction: if writing a report fails, the report can still be reconstructed from the database journal.

## Model Results After Correction

The model was retrained offline using the existing Yahoo cache, without selecting new stocks or optimizing thresholds after the fact to improve scores. It remains a HistGradientBoostingClassifier with 10 instrument-specific features + 9 market-context features. Evaluation used 12 expanding walk-forward folds, 37,279 usable training/evaluation records, and 13,424 out-of-time predictions across folds, covering 2018-05-25 through 2026-09-03.

| Metric | Result |
| --- | ---: |
| ROC AUC | 0.5137 |
| Accuracy | 51.59% |
| Brier score | 0.2518 |
| Log-loss / training-set baseline | 0.6970 / 0.6926 (model worse) |
| Cumulative return at 5 bps round-trip cost | +1.15% |
| Sharpe / maximum drawdown at 5 bps | 0.066 / -26.01% |
| Cumulative return at 0 / 10 / 20 bps | +50.55% / -32.05% / -69.34% |
| Automatic Paper signal admission | Failed |

Interpretation: ranking performance slightly above 0.5 does not establish a statistically credible, tradable edge. Probability quality did not beat the baseline, and results are highly sensitive to costs. The admission thresholds themselves are research conventions, not industry certification.

These are theoretical open-to-close results for six instruments: SPY, JPM, XOM, WMT, JNJ, and BTC-USD. The research convention of fixed capital allocations and daily rebalancing does not fully model cash/margin, securities borrowing, real-time fills, funding rates, or market impact. In particular, the BTC label execution-timing issue has not yet been resolved through retraining, so these figures must not be treated as an executable strategy backtest. The report's +212.80% baseline is intraday long exposure under the same capital-allocation convention, not buy-and-hold.

Both features and return calculations changed between the old and new results, so changes in the figures cannot be attributed to "model improvement." The historical sample has already been examined repeatedly across research rounds and cannot continue to be presented as a fresh final holdout.

Reproduction commands (these overwrite the default v2 candidate files; use a new output name for new experiments):

```bash
.venv/bin/python main.py ml2 --offline
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
```

Detailed evidence is in `artifacts/yahoo_walkforward_v2_model.json`; individual predictions are in `artifacts/yahoo_walkforward_v2_oos.csv`. Model metadata and joblib are paired by SHA-256. Load only files you trust: a hash is not an isolation sandbox for malicious pickle/joblib files. `daily_pit_v2` names a feature contract; it does not mean Yahoo history has become strict PIT data.

## Actual Runs and Data Migration in This Review

- Backup: `artifacts/review-backup-KwMivkOT/` retains the old database, model, metadata, shadow report, and README; none of this research evidence was deleted.
- The warehouse was migrated to schema v4. Ambiguous old SEC/FRED records remain in legacy tables and are excluded from new as-of queries. Both current data categories contain 0 rows.
- All 70,712 old Yahoo `adjusted-v1` rows remain stored but are excluded from strict as-of queries. New snapshots become available only at their actual first observation time; vendor versions that were never recorded in the past cannot be recovered.
- News contains 2,500 first-seen metadata records, but no article bodies, sentiment features, or news signals actually used by ML. Fundamentals, macro vintages, and historical index membership have not yet been populated and connected to the model.
- One `shadow cycle` was actually run. It downloaded market data and wrote the local journal/report only; no trading endpoints were called.
- The old 6 shadow records lacking the new contract or a predefined target remain as `INVALIDATED_LEGACY`, with their original states stored in `legacy_status`; they are excluded from new performance results.
- Added 5 v2 PENDING signals: features through 2026-09-04, with the target fixed to 2026-09-08 using the XNYS calendar. SPY/JPM/XOM/WMT/JNJ were all FLAT. No new samples have settled yet, so forward returns cannot be reported.
- BTC was explicitly skipped. Both effective Paper gates were verified closed, and the existing order audit chain passed verification. The original daily local shadow schedule was retained; no scheduled task was added or changed in this review.

## Recommended Order of Next Steps

### 1. Freeze the Research Protocol and Executable Labels First

First establish a complete, trustworthy workflow for daily US equities: define decision times, entry/exit windows, executable quotes, and latency; set the experiment budget, success criteria, cost scenarios, and final holdout before choosing models. Design a separate BTC target with delayed entry after the bar is complete; do not reuse stock-direction labels for options. The current 0.55/0.45 thresholds have not been optimized for return utility or calibration and should not be treated directly as trading rules.

### 2. Add Genuinely Available, Reproducible Data

Populate the required SEC/ALFRED fields first and align entities and timestamps. News needs trustworthy historical first-arrival times, deduplication, company-entity links, and versions. Any future news model also requires checks on its training cutoff and memory of historical facts. Market data needs delistings, name changes, historical membership, corporate actions, and data-licensing records. Yahoo adjusted prices backfilled today, today's index constituents, or today's large-model answers cannot substitute for information available at the historical decision time.

### 3. Design Model Experiments That Can Demonstrate Incremental Value

Consider conditional after-cost return/risk as a target rather than comparing only directional accuracy. Establish fixed baselines such as constant probabilities, linear/logistic regression, and the current tree model. Use temporal inner-loop selection and outer-loop validation, adding attribution by asset/year/market regime, probability calibration, block-bootstrap uncertainty intervals, and multiple-testing controls. Any new model must outperform under the same predeclared methodology and then remain frozen for forward observation. Repeatedly tuning until a poor historical test passes is not acceptable.

### 4. Then Add Production-Grade Portfolio and Execution Controls

Build a unified account-level order state machine covering position and active-order reconciliation, trading-update streams, reconnection, account/asset permissions, invalid-quote and feed-outage protection, daily risk resets, a manual kill switch, external audit anchors, and recovery drills. The current API-key-level local lock cannot replace account-level coordination across keys or hosts; migrating incident blocks after key rotation also requires an operating procedure.

The portfolio layer needs to address correlation, concentration, volatility targets, margin, securities borrowing, capacity, tail risk, and stress scenarios. Options additionally require complete chains/quotes, contract lifecycles, American-style exercise/assignment, Greeks, dividends, and interest-rate curves; the current Black-Scholes implementation is only a synthetic-data check. High-frequency research also requires sequenced tick/order-book data, clock synchronization, latency distributions, and queue models. Yahoo daily bars or a local scheduled script do not constitute HFT.

### 5. Expand Paper Trading Only Afterward

For now, retain forward shadow observation without submitting orders. After timing and data acceptance checks are complete, separately approve bounded Paper interface acceptance testing and a longer observation period. Passing engineering tests is not strategy admission. Alpaca explicitly states that Paper does not fully reflect market impact, latency slippage, queue position, and other factors, and simulated fills are not constrained by the visible NBBO quantity. Paper profits therefore do not guarantee live returns. [Official Alpaca Paper documentation](https://docs.alpaca.markets/us/docs/paper-trading)

## Responsibilities of Each Section

| File/directory | Responsibility and boundary |
| --- | --- |
| `domain.py` / `options.py` | Decimal trading objects, multiplier accounting, and simplified option pricing; not a production options risk system |
| `data.py` / `strategy.py` | Reproducible synthetic quotes and an order-book baseline; not historical market evidence |
| `risk.py` / `execution.py` / `engine.py` | Local risk controls, latency-aware matching, event-time processing, and accounting |
| `audit.py` | Chain-corruption detection, concurrency locking, and durable writes; cannot guarantee tamper protection without an external anchor |
| `alpaca_paper.py` / `alpaca_cli.py` | A bounded manual Paper channel and order lifecycle; does not run automated ML trading |
| `sources/` / `warehouse.py` | External-data parsing, version storage, conservative availability times, and as-of queries |
| `ml/yahoo.py` / `features.py` / `regime.py` | Yahoo research caching, instrument-specific features, and market-context alignment |
| `ml/training.py` / `walkforward.py` / `metrics.py` | Baseline training, temporal validation, consistent return/cost methodology, and admission reports |
| `sessions.py` / `scheduler.py` | Trading calendars, completed-bar requirements, effective gates, and process locks |
| `shadow.py` / `shadow_cli.py` | Frozen-model inference, predeclared targets, an idempotent journal, and future settlement; no order submission |
| `tests/` / `requirements-tested.txt` | Regression tests and the environment-version record for this review; the latter is not a hashed cross-platform lockfile |

## Official Technical References Checked

HistGradientBoosting's `early_stopping='auto'` and validation-set parameters informed this review of internal validation behavior. The project disables it and uses a fixed iteration count while retaining temporal outer splits. [Official scikit-learn API](https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.HistGradientBoostingClassifier.html)

SEC Company Facts provides units, periods, and filing context. The project's local-midnight availability rule is a conservative modeling choice, not an exact publication time guaranteed by the SEC. [SEC EDGAR API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)

FRED observations parameters define output_type, real-time intervals, limit/offset, and related settings. The project retains versions and validates pagination rather than assuming that a single response contains complete history. [Official FRED API](https://fred.stlouisfed.org/docs/api/fred/series_observations.html)
