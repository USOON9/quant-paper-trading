# Bounded Research Data Refresh

This workflow fills explicitly requested research-data gaps. It is separate from the [offline evidence packet builder](research-evidence.md): refresh may contact approved data sources and append observations to the research warehouse, while evidence generation remains read-only and offline.

The initial scope is one fresh, two-year daily Yahoo snapshot for JPM. SEC Company Facts and FRED/ALFRED are optional sources and may be contacted only when their local configuration is valid. A missing credential or contact identity does not authorize switching providers, scraping an alternative site, changing the evidence availability mode, or substituting current values for historical observations.

No model is trained, deserialized, promoted, or approved by this step. No trading signal, order, fill, sentiment score, or claim of improved investment performance is produced. Daily adjusted bars are research context, not high-frequency execution data.

## Run it

```bash
# Configuration-only check: no provider, database, or broker connection.
.venv/bin/python main.py refresh-data status

# Use a fresh run directory for every attempt.
.venv/bin/python main.py refresh-data run \
  --symbol JPM \
  --asset-class equity \
  --series DFF DGS10 CPIAUCSL UNRATE \
  --sources yahoo sec fred \
  --period 2y \
  --run-dir artifacts/research-refresh/jpm-example-001

# Verify archived hashes and receipt/report alignment without a provider or database.
.venv/bin/python main.py refresh-data show \
  --run-dir artifacts/research-refresh/jpm-example-001
```

After package installation, `quant-refresh-data` provides the same interface. The refresh supports one symbol, at most four explicit macro series, and a period of `1y` or `2y`. The default database is `data/research.duckdb`; `--db` may select another existing schema-v4 `.duckdb` file under the project's `data/` directory. Database and output paths must not contain symlinks or traversal. Refresh does not create or migrate a database implicitly; all required warehouse tables and source-audit columns must already exist.

Install the `data` extra for this workflow; it includes the Yahoo collector dependency. `run` returns exit code `0` only for `COMPLETE` and `3` for a finished `PARTIAL` report, so a failed source does not silently satisfy a shell success chain. Invalid commands or unhandled workflow failures return `2`. `show` returns `0` for a verified archive even when its recorded source status is partial; it verifies history rather than rerunning the refresh. Inspect `status` source readiness explicitly: its zero exit code means the configuration check ran, not that every source is configured or authenticated.

Configuration comes from the project's fixed local `.env` configuration and effective environment. There is no `--env` override for redirecting the refresh to a different credentials file. The status command reports configuration readiness without printing credentials or the SEC contact value. An optional source without valid configuration is skipped explicitly; other requested sources may still complete.

## Preservation boundaries

Each refresh owns a new child directory under `artifacts/research-refresh/`. Existing or interrupted runs must not be overwritten; use a fresh directory for another attempt. Downloaded Yahoo files belong to that run, not to `data/yahoo/`, which may contain inputs used by existing models.

The research warehouse is the intended write target. Existing model files, training datasets, frozen study evidence, shadow journals, broker state, `.env`, trading gates, and schedules are outside the refresh scope. The workflow must not use a broad warehouse-ingestion command that scans unrelated CSV files or automatically resolves cross-dataset aliases.

A completion marker certifies a finished archive, not success for every requested source. A verified archive may still report `PARTIAL`. The marker is written last, and verification rejects missing or changed files. Filesystem artifacts and database commits are separate resources: preserving an interrupted run is necessary for diagnosing a failure after a database batch has committed.

Each run retains `request.json`, incremental `receipt-01.json` through the requested source count, `report.json`, and a final `completion.json`. A received Yahoo snapshot also has an isolated `yahoo.csv`. Receipts retain source identity, fetched and inserted counts, outcome status, and successful ingestion IDs and hashes. The request records the builder-source fingerprint and exact date bounds. Verification checks artifact hashes, expected receipts, request/report alignment, source outcomes, and safety flags without reopening the warehouse.

## Source configuration and data meaning

| Source | Configuration and bounded purpose | What the records establish |
| --- | --- | --- |
| Yahoo Finance | One JPM request for two years of daily history; no broker credentials are needed. | A locally observed adjusted-price snapshot, with completed bars and validated OHLCV. |
| SEC Company Facts | A genuine `SEC_USER_AGENT` identifying the application and a contact email. The example placeholder is not configured contact information. | Selected filing facts with accession, reporting period, unit, and a conservative filing-date availability convention. |
| FRED/ALFRED | A locally configured `FRED_API_KEY` and explicit series IDs. | Observation values and their reported real-time vintage dates, retaining null values as missing. |

The configuration template is `.env.example`. Do not paste keys or personal contact values into source code, commands saved in documentation, Git commits, reports, or chat messages. Reports should show configuration status, never the underlying value. Alpaca credentials are not needed for the Yahoo refresh and must not trigger a news fetch or a broker connection.

The refresh uses a dedicated strict Yahoo snapshot collector rather than the older model-cache downloader. It requests adjusted daily history without automatic repair and rejects malformed or missing required OHLCV, duplicate timestamps, nonfinite prices, and inconsistent price bounds instead of silently repairing or filling them. A bar is conservatively complete only when its timestamp plus 24 hours is at or before response observation; excluded incomplete rows are counted. The saved snapshot is normalized tabular data, not an untouched provider response; its hash must not be described as a hash of the original HTTP response. The source content hash also binds the request and observation time, so a new receipt hash alone does not imply changed economic values.

The existing SEC connector selects a bounded set of accounting concepts, not every field in every filing. Similar-sounding revenue concepts are not automatically combined, and quarterly and year-to-date periods remain distinct. A ticker resolved to a current CIK is not a historical security-master guarantee. The explicit evidence IDs `YF:JPM` and `US:JPM` remain separate source identifiers.

The FRED connector preserves observation/vintage pairs, checks pagination consistency, and rejects conflicting values for the same pair. Refresh bounds both the observation-date range and the real-time/vintage query range to the same window: 365 days for `1y` or 730 days for `2y`, ending at the refresh start's UTC date. These are fixed day counts, not calendar-year offsets. The request archive records `macro_observation_start`, `macro_realtime_start`, and `macro_realtime_end`; stored observations and returned vintage dates must stay within the requested bounds. Each series is limited to 10 pages and 200,000 records; incomplete pagination is rejected rather than presented as a complete batch.

FRED may return an earlier period-boundary observation. Every returned row is parsed and validated before observations preceding the explicit observation start are filtered out. Malformed values, conflicting records, invalid vintage bounds, and future dates still fail validation, including on rows that would otherwise be excluded. Safe request metadata records provider and parsed record counts and `excluded_before_observation_start`; the content hash covers all combined provider observation rows, including exclusions. An entirely filtered result retains this metadata and hash in its `EMPTY` receipt without writing to the database. This is explicit boundary filtering, not frequency inference, missing-value filling, or vintage backdating.

A bounded real-time query can clip a returned interval's first `realtime_start` to the requested window boundary. Such a value is a provider-reported known-as-of bound within this query, not necessarily the observation's original publication date. This refresh does not claim access to older vintages outside the window, and it must not relabel a clipped boundary as an exact historical release timestamp.

SEC/FRED transport uses fixed official HTTPS hosts, rejects redirects and ambient proxies, caps response sizes, and performs no automatic retries. It supplements Python's default certificate authorities with the installed `certifi` bundle while requiring both certificate and hostname verification. Missing trust roots or failed certificate verification never authorize an insecure retry. Its 30-second timeout is a transport timeout, not a guaranteed whole-refresh deadline. These controls do not prove complete economic coverage. Warehouse v4 does not store authoritative macro series units or frequency; the refresh must not infer them or derive signals from mixed units.

## Observation time is not the event date

Yahoo adjusted history can change after splits, dividends, and provider corrections. A two-year download observed today is available to this system today, not two years ago. Newly stored Yahoo rows use observed `snapshot-v2` versions. Legacy `adjusted-v1` rows remain excluded from evidence packets; refreshing does not relabel those old rows as historically known data.

For unchanged Yahoo values already stored as observed snapshots, retaining the first observation is preferable to creating a new version solely because the file was downloaded again. An unchanged refresh can therefore fetch records while inserting none. Changed values require a new observed version; the earlier version remains part of the warehouse history.

SEC filing dates and FRED returned real-time dates describe provider history under the requested bounds. The existing connectors use explicit, conservative date-level conventions: SEC facts become available after the filing day ends in `America/New_York`; FRED observations become available after the returned vintage/known-as-of day ends in `America/Chicago`. These are assumptions recorded with the data, not measured intraday publication times; a window-clipped FRED boundary does not establish the original release date. Local ingestion occurs when the system actually receives and stores the records.

The evidence availability mode remains an independent, explicit choice:

| Mode | Eligibility at an exclusive cutoff | Consequence of a refresh performed later |
| --- | --- | --- |
| `local_observed` | Both `available_at < as_of` and `ingested_at < as_of`. | Newly ingested records do not appear in a packet for an earlier local cutoff. |
| `reconstructed` | `available_at < as_of`. | Later-ingested SEC/FRED history may support a reconstruction, not proof of a historical forward experiment. Newly observed Yahoo snapshots still cannot be backdated. |

A source refresh must not silently change this mode to make a historical packet look more complete. To inspect newly received data in local-observed mode, build a new packet with an explicit cutoff strictly after successful ingestion. Preserve the old packet and its missing-data findings.

## Interpret outcomes precisely

| Outcome | Meaning | What it does not mean |
| --- | --- | --- |
| `MISSING_CONFIGURATION` | The source was not contacted because required local configuration was absent. | The provider has no relevant records. |
| `INVALID_CONFIGURATION` | A supplied contact or key failed local format checks, including placeholder detection. | Credentials were sent to the provider or verified as authorized by it. |
| `UNSUPPORTED` | The requested source/asset combination is not supported by this bounded workflow. | The workflow will substitute a different instrument or provider. |
| `EMPTY` | A completed source response yielded no eligible records for the exact requested scope. | No relevant event or economic fact exists elsewhere. |
| `FAILED` | Network access, provider response validation, pagination, or record validation did not complete successfully. | Cached data is automatically an acceptable replacement. |
| `NO_NEW_ROWS` | Valid records were received, but no additional rows were inserted. | The database is byte-unchanged: FRED real-time interval metadata can still update, and a new ingestion audit row is recorded. |
| `UPDATED` | Valid records were stored and their ingestion was recorded. | Coverage is exhaustive, historically forward-available, fresh enough for execution, or predictive. |
| Partial run | Some requested source work succeeded while other work was skipped or failed. | All sections are now populated or the research model is ready. |

Fetched and inserted counts answer different questions and should remain separate. Provider exceptions must be sanitized: a FRED request URL can contain its API key, and raw exception text must not be copied into reports or terminal summaries.

For each source batch, its provider request finishes before a writable database connection is opened, avoiding a database write lock held while waiting on that request. Source batches and their ingestion audit rows are committed together. A failure in one source remains distinguishable from a successful commit by another; a fresh source receipt records what was attempted even when no new rows were inserted.

## Implementation map

| Module | Responsibility |
| --- | --- |
| `src/quantpaper/research/refresh.py` | Coordinates bounded source requests, source outcomes, database ingestion, and isolated refresh artifacts. |
| `src/quantpaper/research/refresh_cli.py` | Provides configuration-only status and the explicit refresh command with sanitized summaries. |
| `src/quantpaper/sources/yahoo_snapshot.py` | Collects strict, non-repaired completed daily Yahoo snapshots without using the model-input cache. |
| `src/quantpaper/sources/http.py` | Supplies bounded HTTP transport for the SEC/FRED source requests. |
| `src/quantpaper/sources/sec.py` | Resolves a requested ticker and parses selected SEC Company Facts with filing provenance. |
| `src/quantpaper/sources/fred.py` | Fetches explicit FRED series with vintage-aware pagination and key-free request metadata. |
| `src/quantpaper/warehouse.py` | Validates and ingests source records, retaining observed Yahoo versions and source ingestion records. |
| `src/quantpaper/research/evidence_store.py` | Reads eligible warehouse rows in a separate read-only transaction after refresh. |
| `src/quantpaper/research/evidence.py` | Builds a research-only evidence packet with explicit coverage, time semantics, and record hashes. |

## Verification and publication

Synthetic regression tests cover output isolation, missing configuration, empty responses, provider failures, no-new-row updates, transaction rollback, observed Yahoo availability, and unchanged protected model inputs. They also check that absent writer tables fail before network calls and that refresh never invokes the migrating warehouse initializer. Tests use temporary databases and mocked providers rather than the user's credentials or live services.

### Verified local run: 2026-09-07

Four create-only attempts were retained and their archive hashes and receipt/report alignment verified. Earlier partial attempts remain partial: the initial missing certificate trust roots, an overbroad FRED real-time range, and provider observation-boundary rows were diagnosed and corrected. Subsequent attempts requested only the sources that had not yet succeeded. No successful batch was replaced or silently relabeled.

| Source / identifier | Newly inserted records across successful batches | Meaning |
| --- | ---: | --- |
| Yahoo / JPM | 502 | Completed daily bars in a newly observed two-year adjusted snapshot. |
| SEC / JPM | 1,247 | Selected accounting facts, not distinct filings or companies; the Yahoo/FRED lookback does not restrict the Company Facts history. |
| FRED / DFF | 727 | Observation/vintage records, including any missing values. |
| FRED / DGS10 | 519 | Observation/vintage records, including any missing values. |
| FRED / CPIAUCSL | 39 | Observation/vintage records; three validated earlier-boundary records were explicitly excluded. |
| FRED / UNRATE | 27 | Observation/vintage records; one validated earlier-boundary record was explicitly excluded. |

The final FRED-only attempt was `COMPLETE` for its two requested series. This is not a claim that the preceding partial attempts, the evidence packet, or all research coverage became complete. All four macro series succeeded across the retained attempts. Macro observations and real-time queries were bounded to `2024-09-07` through `2026-09-07` for the successful batches.

A new offline `local_observed` evidence packet was built and verified with an exclusive cutoff of `2026-09-07T20:05:39.495182+00:00`. Market, fundamentals, and all four macro sections now contain eligible records. The 500-record per-section cap truncates market, fundamentals, DFF, and DGS10. Selecting the latest eligible vintage leaves 22 CPIAUCSL and 23 UNRATE observations; null values remain explicitly missing. Two eligible instrument identity rows overlap, and the 155 existing news records contain metadata and hashes only. The packet therefore still marks every section `PARTIAL`; no automatic identity resolution, text reconstruction, or imputation was performed.

All 505 local regression tests passed after these changes, including empty-result provenance, partial-run exit codes, and rejection of boolean SEC/FRED values before numeric conversion. These final hardening changes were tested with synthetic inputs; the successful live batches were not refetched, and no claim is made that those batches contained boolean values. A before/after fingerprint check confirmed all 15 protected items were unchanged: existing Yahoo model-input files, existing model artifacts, the local `.env`, and the shadow-signal journal. The research warehouse intentionally changed through the audited source batches. No broker connection, order, model training, model promotion, trading-gate change, or scheduling change was performed.

Only code, tests, safe configuration templates, and English documentation belong in GitHub. Downloaded snapshots, database contents, generated reports, API credentials, contact details, and other local research artifacts remain private and Git-ignored. Hashes identify ordinary changes; without an independent trusted anchor they do not prevent a complete archive and its hashes from being rewritten.

After the refresh is verified, the next check is a new offline evidence packet showing which gaps actually closed and which remain. Additional data alone does not improve the current model: any later modeling change still requires a frozen research protocol, leakage checks, and out-of-sample comparison against the existing baseline.
