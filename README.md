# Multi-Asset Quant Paper HFT

An event-driven, multi-asset paper-trading research framework. Local synthetic simulations cover US equities, cryptocurrencies, and options; daily ML research covers stocks/ETFs and BTC; the current forward shadow process generates observation-only signals for US stocks/ETFs. This is not a validated trading system. The default runs only deterministic local simulations, and the order adapter is restricted to Paper mode.

> Here, "high frequency" refers to millisecond events, short-horizon signals, and latency-sensitive research workflows—not exchange colocation, kernel bypass, or microsecond production HFT. Returns from synthetic demonstration data have no investment significance.

## Structured research evidence and automated checks

The [research evidence layer](docs/research-evidence.md) now packages existing warehouse identity, observed daily bars, filing facts, macro vintages, and news metadata into deterministic, timestamped research inputs. The default requires both availability and local ingestion strictly before the requested cutoff. Missing data, ambiguous identity, bounded coverage, and metadata-only news remain explicit; no current information is backfilled into historical evidence.

`python main.py evidence build --help` describes the offline builder. Outputs are create-only under `artifacts/research-evidence/`, with record IDs, snapshot/packet hashes, a builder manifest, and a verified English report. This does not call an LLM, train or promote a model, load credentials, modify the warehouse, or submit orders. Existing trading gates and schedules remain unchanged.

GitHub CI is configured for Python 3.11 and 3.14 with mocked/synthetic tests, code-only secret checks, and a launcher smoke test. See [publication security](docs/github-security.md) for its boundaries. A passing software test does not validate investment performance.

## Bounded research-data refresh

The [research refresh workflow](docs/research-data-refresh.md) adds explicitly requested Yahoo snapshots, SEC filing facts, and FRED/ALFRED vintages to the existing research warehouse. Start with `python main.py refresh-data status`, then inspect `python main.py refresh-data run --help`. Source configuration stays in the local `.env`; output reports never include API keys or the SEC contact identity.

Each attempt uses a new directory under `artifacts/research-refresh/`. Yahoo collection does not repair or fill missing prices and does not overwrite `data/yahoo/` model inputs. SEC identity and FRED pagination/date bounds are checked before ingestion; successful data and audit rows commit together per source. Failures and missing configuration remain explicit. Existing databases must already have the full schema: the refresh does not create or migrate them. This changes research data, not trained models, shadow signals, schedules, or trading permissions.

## Status after the 2026-09-05 review

See the [review report](docs/quant-review-2026-09-05.md) for findings, fixes, evaluation conventions, and remaining work. The current model still fails admission: 12-fold out-of-sample AUC is 0.5137; at 5 bps round-trip cost, cumulative return is approximately 1.15%, Sharpe 0.066, and maximum drawdown approximately -26.01%. These are theoretical intraday research results across six assets, not realized trading returns. At 10 bps, return is approximately -32.05%. Corrected statistical conventions are not evidence of an improved or profitable model.

Both order gates remain `NO`. No orders were submitted during this review. Backups of the previous model and database are stored in `artifacts/review-backup-KwMivkOT/`. The 6 legacy shadow records are retained as `INVALIDATED_LEGACY`; new v2 records declare their target trading session in advance. BTC requires separate delayed-entry labels and is temporarily excluded from daily shadow generation.

The tested environment snapshot is in `requirements-tested.txt`; do not upgrade dependencies blindly in an active environment. The complete test suite requires `.[ml,paper,data]`.

## 2026-09-07: Independent v3 timing-target experiment

The [v3 experiment and results](docs/research-v3-2026-09-07.md) are complete: US equities and BTC are trained separately; BTC uses the complete D−2 daily bar to predict the target day, with explicit decision, entry, exit, and label-availability times. Three baselines, long/flat rules, and cost stress scenarios are fixed in advance. Results are written only to a new child directory of `artifacts/research-v3/`; **the v2 observation model was not replaced, and trading was not enabled**.

The US-equity tree model produced research cumulative return of +12.79% and Sharpe 0.373 at 5 bps, but return fell to -5.59% at 10 bps. The BTC tree model returned -72.83% at 25 bps and remained negative even at zero cost. The two groups use different windows and must not be pooled or directly compared with the old v2 figures. This remains research using daily price proxies, not a validated executable backtest. At this stage, all 128 project tests passed.

```bash
# View completed results without retraining or submitting orders:
.venv/bin/python main.py research show --run-dir artifacts/research-v3/20260907-timing-baseline
```

The v3 document describes the new modules, experiment protocol, all candidates, and subsequent data acceptance requirements.

## 2026-09-07: Read-only minute-bar and quote audit

The [market-data connection, cost sample, and module walkthrough](docs/marketdata-audit-2026-09-07.md) are complete. Historical SIP data for five stocks/ETFs and Alpaca US data for BTC/USD were collected: 29,616 quotes and 3,267 valid in-session minute bars. JNJ had 2 missing minutes; BTC had 121 missing minutes and 921 zero-volume bars. Quality warnings are retained, with no automatic gap filling.

This is a single-session connectivity and data diagnostic, **not proof of real-time SIP entitlement, trade executions, or strategy returns**. No orders were submitted, models updated, or continuous collection started. Existing models, the database, and automation were unchanged. At this stage, all 175 project tests passed.

```bash
# Verify and view the saved market-data report without network access:
.venv/bin/python main.py marketdata show --run-dir artifacts/marketdata/20260907-sip-crypto-audit
```

## 2026-09-07: Five-day, three-window read-only market-data study

The [multi-session market-data results and new modules](docs/marketdata-study-2026-09-07.md) are complete: 5 days per asset, with quote windows near the open, at midday, and near the close. All 120 segments returned with complete pagination, preserving 206,663 quotes and 16,577 valid in-session minute bars. Statistics are equally weighted by day within each asset/window, with 0/250/1000 millisecond historical event-time offset diagnostics. **These are not measured execution latencies, fill replays, or strategy returns**.

BTC had 369 missing minutes; a valid midday quote could be selected within the predefined interval on only 3/5 days. JNJ had 4 missing minutes. Models, the database, and existing automation were unchanged; order gates remained closed. At this stage, all 231 project tests passed.

```bash
.venv/bin/python main.py marketdata show --run-dir artifacts/marketdata/20260907-five-session-study
# Use a new directory name for another one-time capture:
.venv/bin/python main.py marketdata study --sessions 5 --stock-feed sip --run-dir artifacts/marketdata/NEW-STUDY-NAME
```

## 2026-09-07: As-of quote reconstruction and offline replay

The [replay rules, actual results, and new modules](docs/asof-replay-2026-09-07.md) are complete. Reusing the five-day study's 90 target times, an additional 34,555 quotes were collected from 5 seconds before to 2 seconds after each target. Only the latest event strictly before the evaluation time is used. Invalid or ambiguous updates invalidate the previous quote; a missing or stale decision quote cannot be rescued by future quotes.

Under the fixed research assumption of a maximum quote age of 1 second, 66/90 decision times passed quote checks, while 24 were blocked for missing or stale quotes. Across the 0/250/1000 millisecond scenarios, 193 reference-quote checks passed and 77 were blocked; **these are not orders, fill rates, or returns**. Fully offline recomputation matched the initial results item by item. No orders or simulated fills were recorded, and models and the database were unchanged. At this stage, all 293 project tests passed.

```bash
# Verify and view completed replay results offline:
.venv/bin/python main.py marketdata show --run-dir artifacts/marketdata/20260907-asof-offline-check
# Another offline recomputation requires a new output directory; no API access:
.venv/bin/python main.py marketdata replay --source-dir artifacts/marketdata/20260907-asof-preroll --run-dir artifacts/marketdata/NEW-OFFLINE-REPLAY
```

## 2026-09-07: Offline intent admission and lifecycle audit

The [intent admission rules, module responsibilities, and usage instructions](docs/intent-admission-2026-09-07.md) are complete. For the previous stage's 90 historical target times, 540 synthetic BUY/SELL intents of a fixed 5 dollars each were generated across 0/250/1000 millisecond scenarios, producing 1,620 hash-linked audit events. The current frozen model still fails admission, so **all 540 intents were rejected**. Missing or stale quotes and other data issues are accumulated separately rather than hidden by the model rejection.

This is an offline engineering check, not model predictions, actual orders, or executions. Only the model's file hash and current approval status are checked; it is neither deserialized nor trained. Network requests, orders submitted, and simulated fills are all 0. Existing models, the database, evidence, and automation were unchanged. All 359 project tests and the dependency consistency check passed.

```bash
.venv/bin/python main.py marketdata show --run-dir artifacts/marketdata/20260907-intent-admission
```

GitHub contains only source code, tests, configuration templates, and documentation. `.env`, market data, models, and audit artifacts are not uploaded. Run the [credential and staging checks](docs/github-security.md) before publication. The historical report commands above require existing local evidence; cloning the source alone does not provide those datasets.

## Quick start

The simplest option is to run directly from the project directory:

```bash
python main.py
python main.py --events 3000 --audit artifacts/my-run.jsonl
```

Alternatively, install it as a standard Python package:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
quant-paper demo --events 1500
quant-paper demo --events 1500 --audit artifacts/demo-audit.jsonl
python -m unittest discover -s tests -v
```

If macOS has no system Python, use the Python runtime provided in the Codex workspace and set the source path:

```bash
PYTHONPATH=src /path/to/python3 -m quantpaper.cli demo --events 1500
PYTHONPATH=src /path/to/python3 -m unittest discover -s tests -v
```

## What each section does

### 1. `domain.py` — Trading domain model

Defines asset types, quotes, orders, fills, positions, and cash accounts. Prices, quantities, and cash use `Decimal`; options use `multiplier=100` for the contract multiplier. This is the shared vocabulary for backtesting, paper trading, and any future live-trading implementation.

### 2. `data.py` — Reproducible market data

Generates quotes with nanosecond timestamps for stocks, BTC, and options. A fixed random seed produces identical event streams on every test run, making regressions easier to detect. This supports engineering validation only, not evidence of strategy effectiveness.

### 3. `strategy.py` — High-frequency research baseline

Uses bid/ask size imbalance and very short-term momentum to form signals, then calculates target positions from current inventory. The strategy generates order intents only; it does not connect directly to a broker or bypass risk controls.

### 4. `risk.py` — Independent risk controls

Orders in the local simulator are checked against order notional, per-asset exposure, total exposure, orders per second, and loss limits for the run. Unfilled orders also reserve risk capacity. A kill switch activates at the loss limit. This is not a complete multi-day production risk system: session resets, margin, and automated risk reduction still need separate designs. Manual Alpaca tests use a different, tightly bounded order lifecycle rather than this simulated account.

### 5. `execution.py` — Local paper matching

Simulates network/processing latency, bid/ask spread, slippage, fees, and order states. Stocks, cryptocurrencies, and options use different fee models. This is more conservative than a simple backtest that assumes immediate fills at the current midpoint, but it still cannot fully reproduce real queue dynamics or market impact.

### 6. `engine.py` — Event loop

Connects market data, fill reports, strategy, risk controls, matching, and account bookkeeping in chronological order. Each signal is generated strictly after receiving the current event to avoid typical look-ahead leakage.

### 7. `options.py` — Option-pricing checks

Provides Black-Scholes prices and Delta/Gamma/Vega, primarily for synthetic data and sanity checks. US equity options are generally American-style; actual trading relies on market quotes. This function is not a complete production pricer.

### 8. `reporting.py` — Results reporting

Reports equity, PnL, return, maximum drawdown, fill count, rejected orders, and fees. After real historical data is integrated, reporting can be extended with Sharpe, execution deviations, and attribution by asset and strategy.

### 9. `alpaca.py` — Restricted paper gateway

The legacy gateway retains read-only functionality. Its direct submission and cancel-all methods are disabled to prevent bypassing lifecycle audits. Restricted manual Paper tests go through `alpaca_paper.py`, with fixed Paper mode, two gates, precise state checks, per-key locking, and persistent blocking after ambiguous failures.

### 10. `configs/paper.toml` — Parameters and risk limits

Centralizes initial capital, latency, slippage, strategy thresholds, and risk limits, separating code review from parameter changes. All default values are for demonstration only.

### 11. `tests/` — Automated verification

Covers bookkeeping, quote/fill causality, pending-order risk reservations, order failures and partial fills, training-feature timing, portfolio-return conventions, data versions, trading calendars, shadow idempotency, and transaction rollback. Broker tests use mocks and are not equivalent to real API integration acceptance tests.

### 12. `audit.py` — Tamper-evident audit chain

Writes events to append-only JSONL, verifies the full hash chain before restarting or appending, and locks and synchronizes writes to disk. It detects ordinary modifications to history but cannot prevent an entire chain from being rewritten without an external trusted anchor. Full-chain scans are also unsuitable for production HFT.

## Safely enabling Alpaca Paper access

1. Copy `.env.example` to `.env` and provide **paper-only** credentials.
2. Initially keep `ENABLE_ALPACA_PAPER=NO` and call only `account()` to verify the account.
3. The current model has not passed admission, so keep the gate closed. The enablement commands below describe independent manual connectivity tests, not approval for automated strategy orders.
4. Never commit API keys to Git.

A restricted `alpaca` CLI is available, but it is not an unattended automated trading system. A continuous trade-update stream, automated recovery state machine, cross-host coordination, and complete account reconciliation remain to be implemented.

## Yahoo Finance machine learning

After installing the optional ML dependencies, download the maximum available daily history for each Yahoo symbol and build a model with a strictly chronological out-of-sample split:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[ml]'
python main.py ml --symbols SPY JPM XOM WMT JNJ BTC-USD --period max
```

The model uses data from the previous trading day and earlier to construct return, volatility, moving-average distance, intraday range, volume-anomaly, and RSI features, predicting the next trading day's open-to-close direction. The final 20% of dates are reserved for out-of-sample evaluation, with a 5-trading-day purge gap between training and testing. Results deduct configurable trading costs and preserve the following auditable files:

- `data/yahoo/*.csv`: Cached source adjusted OHLCV data.
- `artifacts/yahoo_daily_model.joblib`: Model, feature order, symbol list, and training cutoff date.
- `artifacts/yahoo_daily_model.json`: Out-of-sample metrics, data SHA-256 fingerprints, and dependency versions.

The model is marked `approved_for_paper=true` only if it simultaneously achieves `ROC AUC >= 0.53`, `Sharpe >= 0.75`, maximum drawdown no greater than 20%, and positive returns after costs. These are minimum research gates, not evidence that a model is suitable for live trading.

Yahoo daily data is suitable for ML research, not historical HFT analysis. According to the yfinance documentation, intraday data at intervals shorter than one day is limited to the most recent 60 days. Yahoo/yfinance data is used here for personal research only; verify data licensing before any commercial use. Yahoo also does not provide complete historical option chains or order books for long-term training.

## Step 1: Point-in-time data layer

The research warehouse uses DuckDB to retain both event time and actual availability time, preventing later publications or revisions from leaking into backtests. Tables exist for market data, fundamentals, macro data, news, index constituents, and corporate actions. This stage first imports the existing Yahoo daily bars; SEC, FRED, news, and historical constituent sources are integrated separately in subsequent stages.

```bash
python main.py warehouse init
python main.py warehouse ingest-yahoo
python main.py warehouse stats
```

The default database is `data/research.duckdb`. In schema v4, Yahoo snapshots retain this machine's actual first-observed time and content version; adjusted prices backfilled today must not be presented as information known on the historical date. Legacy `adjusted-v1` rows are retained but excluded by `bars_as_of`. The current ML pipeline still reads Yahoo CSV files directly for exploratory research; the warehouse does not automatically make it a strict PIT backtest.

## Alpaca paper trading

Install dependencies and create the local configuration:

```bash
python -m pip install -e '.[ml,paper,data]'
cp .env.example .env
```

Put only Alpaca **Paper Trading** credentials in the local `.env`; never send keys in chat:

```dotenv
APCA_API_KEY_ID=your_paper_key_here
APCA_API_SECRET_KEY=your_paper_secret_here
```

Read-only checks do not submit orders:

```bash
python main.py alpaca status
python main.py alpaca quote --symbol SPY
```

The connectivity test submits a limit order at a very low price and immediately requests its cancellation. It requires explicitly enabling the first Paper gate in `.env`:

```dotenv
ENABLE_ALPACA_PAPER=YES_I_UNDERSTAND
```

```bash
python main.py alpaca submit-cancel --symbol SPY --quantity 1
```

The paper round-trip fill test also requires the second gate and is restricted by code to an open market and 1–25 dollars per test:

```dotenv
ENABLE_ALPACA_PAPER_ROUND_TRIP=YES_RUN_SMALL_ROUND_TRIP
```

```bash
python main.py alpaca round-trip --symbol SPY --notional 5
```

All order lifecycles are written to `artifacts/alpaca-paper-audit.jsonl`. The adapter is fixed to Alpaca's `paper=True`; there is no parameter for switching to a live-trading endpoint.

After a timeout or uncertain submission result, query the original `client_order_id` rather than retrying blindly. A persistent block is stored at the fixed project path `data/paper-state/<key-hash>.reconciliation.json`. Changing the audit-log path cannot clear a block for the same key; legacy markers beside older logs are also recognized. Recovery requires manually reconciling broker orders and positions and retaining evidence before deciding how to proceed. Do not delete markers merely to rerun a test. A partial fill is not a complete fill, and acceptance of a cancellation request is not confirmation of a terminal order state.

## Step 2: Market context and walk-forward validation

The second stage creates a separate `ml2` research pipeline without overwriting the first-stage model:

```bash
python main.py ml2 --symbols SPY JPM XOM WMT JNJ BTC-USD --cost-bps 5
# Recompute from the existing cache without downloading or modifying data:
python main.py ml2 --offline
```

Section responsibilities:

- `ml/features.py`: 10 asset-specific features; all inputs are lagged by one day to predict the next trading session's open-to-close direction.
- `ml/regime.py`: 9 market-context features derived from SPY, QQQ, IWM, VIX, and a US ten-year yield proxy, using backward-as-of joins with conservative availability dates and stale-data limits.
- `ml/walkforward.py`: 12 expanding walk-forward folds, each with a 5-session purge gap before testing and training restricted to earlier data.
- Cost stress tests: Report 0, 5, 10, and 20 bps together rather than showing only the most favorable cost assumption.
- Stability gates: AUC, Sharpe, drawdown, consistency across folds, training-only baseline log loss, and returns at double the assumed cost must all pass.
- `ml/metrics.py`: Fixed equal capital allocations across six assets. An allocation stays in cash when data is missing or the signal is FLAT; equity allocations are not transferred to BTC on weekends. Returns and annualization use calendar days. Cost is explicitly the full round-trip cost of one active signal. The benchmark is intraday long exposure with the same capital allocation, not buy-and-hold.

Output files:

- `artifacts/yahoo_walkforward_v2_model.joblib`: Candidate model with the `daily_pit_v2` feature contract. It must not be connected to automated order submission if it fails the gates.
- `artifacts/yahoo_walkforward_v2_model.json`: Complete metrics, dates and performance by fold, cost stress tests, dependency versions, data fingerprints, and the model SHA-256.
- `artifacts/yahoo_walkforward_v2_oos.csv`: Individual chronologically out-of-sample predictions by fold, allowing independent metric recomputation. After repeated model selection using these results, they are no longer an independent final holdout.

Retraining updates these default candidate files. For new experiment comparisons, use `--model`, `--metadata`, and `--predictions` to specify new filenames and preserve the current frozen model. `shadow` verifies the model/metadata hashes and contracts and rejects silent mixing of old files. Load only trusted local joblib files; matching hashes do not sandbox malicious files.

Yahoo's adjusted historical prices are not a strict point-in-time source database, and the current asset universe has survivorship bias. Even if `ml2` passes its gates, it may proceed only to Paper signal observation, not live trading on that basis.

## Step 3: Fundamentals, macro vintages, and news

The third stage writes external data into the same point-in-time DuckDB, retaining request parameters, record counts, and content SHA-256 for each ingestion. First check the local configuration:

```bash
python main.py sources status
```

### SEC Company Facts

The SEC requires an identifiable User-Agent for automated access. First put your actual contact email in the local `.env`:

```dotenv
SEC_USER_AGENT=QuantPaperResearch your_email@example.com
```

Then ingest facts from filings such as 10-Ks and 10-Qs by stock:

```bash
python main.py sources sec --ticker JPM
```

`sources/sec.py` retains accession numbers, financial-period start and end dates, units, filing dates, and values, preventing quarterly and cumulative figures from overwriting each other. The current path uses filing dates rather than exact acceptance timestamps and conservatively assigns availability to New York local midnight following the filing date.

### FRED/ALFRED

Put your free FRED API key in `.env`:

```dotenv
FRED_API_KEY=your_fred_api_key_here
```

```bash
python main.py sources fred --series DFF DGS10 CPIAUCSL UNRATE GDPC1 VIXCLS
```

`sources/fred.py` uses real-time-period output, validates pagination, and retains vintage start and end dates. Availability is conservatively assigned to Chicago local midnight following the vintage date. API keys are excluded from request audit records, and HTTP errors do not echo request URLs containing keys.

### Alpaca news

The news connector reuses the configured Alpaca Paper data credentials:

```bash
python main.py sources news --symbols SPY JPM XOM WMT JNJ BTCUSD --days 365 --limit 1000
```

`sources/alpaca_news.py` stores only publication time, first-seen time, source, URL, associated symbols, quality flags, and headline hashes. It does not store copyrighted article bodies.

Generate the data-coverage and point-in-time constraints report:

```bash
python main.py sources report
```

Results are written to `artifacts/source-coverage.json`. For historically backfilled news, `first_seen_at` is later than `published_at`. Until data establishing historical delivery times is available, these records must not be used directly in a backtest claimed to be unbiased.

Historical index-constituent tables remain empty: no provider with reliable announcement timestamps and historical-data licensing has been integrated. Backtesting history with today's constituents creates survivorship bias, so the code does not scrape current web constituents and present them as historical records.

## Next step: Forward-only shadow mode

Shadow mode refreshes completed Yahoo daily bars, generates probabilities for the next future session using the frozen model, and writes them to DuckDB. The module imports no broker, order, or trading client and therefore cannot submit orders:

```bash
python main.py shadow run
python main.py shadow report
```

Each signal stores its generation time, feature cutoff, full feature payload/hash, model hash and contract, model training cutoff, admission status, probability, and direction. It declares the next target session and its opening/closing UTC times in advance. The combination of model, asset, and target session is unique; repeated runs across weekends do not create duplicates.

Once the future complete daily bar is available, run:

```bash
python main.py shadow settle
python main.py shadow report
```

The settlement process reads only the target session declared in advance and settles no earlier than 30 minutes after the official close, after OHLC validation. Missing bars remain pending; a later day's bar cannot substitute for the target. Settlement OHLC values and hashes are retained. Signals must be recorded at least 1 minute before the target open; stale features must not be used to predict an already-started session. Probabilities `>=0.55` are labeled LONG, `<=0.45` SHORT, and the middle range FLAT. Active signals deduct the full round-trip cost. Reports group results by model and cost; they do not sum individual signal returns and call that portfolio PnL.

Results are saved in `artifacts/shadow-report.json`, with the underlying journal in the `shadow_signals` table of `data/research.duckdb`. The current candidate model has not passed admission, so these records are for evaluation only and never trigger Paper orders.

### Automated shadow cycle

`python main.py shadow cycle` acquires a single-instance file lock before opening DuckDB, verifies that both effective order gates from environment variables and `.env` are `NO`, refreshes complete daily bars, then processes settlement and new signals using the XNYS calendar. Database settlements and inserts share one transaction; the JSON report is replaced atomically through a temporary file. The database and report do not share a cross-resource transaction. US equities use complete daily bars available 30 minutes after the close. Weekend catch-up runs are allowed, but not for target sessions that have already started. BTC's UTC daily bars have no overnight gap: after waiting for the complete previous-day bar, a fill at the next day's already-past open cannot be assumed. BTC is therefore explicitly skipped until a delayed-entry target is redefined and the model retrained.

The Codex heartbeat `Quant Shadow Daily Cycle` is configured to run this command in the current task at 22:15 London time each day. Notifications are limited to failed runs. The task never modifies order gates or submits Paper or live orders.

This is a local task, not a trading server with a runtime SLA; the existing schedule was retained. Sleep, shutdown, or network failures may cause missed signal-generation windows. Missing observations must not be backfilled with retrospective predictions.
