# Structured Research Evidence Packets

This step adds an offline research input layer, not a new trading model. It organizes existing warehouse records into traceable, bounded evidence sections. It makes no network or LLM calls, does not load `.env`, and cannot submit orders, approve a model, modify the warehouse, or change a trading gate.

The design adopts useful ideas from research-agent systems: deterministic facts before interpretation, explicit missing information, structured output, and reproducible artifacts. No TradingAgents source code or dependencies were copied into this project.

## Run it

Use the existing Python environment with the project's `data` extras installed. Do not upgrade an active environment just to generate a report.

```bash
# Explicit source identifiers; do not infer security identity from similar names.
# Replace the example cutoff with the decision timestamp you intend to inspect.
.venv/bin/python main.py evidence build \
  --db data/research.duckdb \
  --instrument-id YF:JPM \
  --fundamental-instrument-id US:JPM \
  --news-entity JPM \
  --series DFF DGS10 CPIAUCSL UNRATE \
  --as-of 2026-09-07T12:00:00Z \
  --availability-mode local_observed \
  --max-records 100 \
  --run-dir artifacts/research-evidence/jpm-example-001

# Verify archived evidence without opening the database or requiring old inputs.
.venv/bin/python main.py evidence show \
  --run-dir artifacts/research-evidence/jpm-example-001
```

After a normal package installation, `quant-evidence` provides the same interface. The launcher form above works without reinstalling the package. Each build requires a new run directory. An interrupted or completed run is never overwritten; use another name to retry.

`--fundamental-instrument-id` and `--news-entity` are optional. Omitting them reports `NOT_REQUESTED`, rather than guessing an alias. Passing `--series` with no following values disables macro queries. `--max-records` is a limit of 1 to 500 **per section and per macro series**, not a global limit. Up to 20 explicit macro series may be requested.

## Time semantics

| Mode | Required conditions | Interpretation |
| --- | --- | --- |
| `local_observed` (default) | `available_at < as_of` and `ingested_at < as_of` | Rows the local warehouse had received strictly before the requested cutoff. |
| `reconstructed` | `available_at < as_of` | Provider-history reconstruction, potentially ingested after the historical cutoff. Not proof of a forward experiment. |

All cutoffs require an explicit timezone and support at most microsecond precision, matching the warehouse timestamp representation. Naive timestamps, date-only values, nanosecond timestamps, and future cutoffs are rejected rather than rounded or guessed. A record exactly at the cutoff is excluded conservatively.

Availability and ingestion filters apply **before** selecting the latest known revision. This prevents a later amendment from replacing or hiding the earlier version that was available at the cutoff. Generating a packet today for an old cutoff does not establish that the packet or a model prediction existed then; every packet explicitly has `forward_prediction: false`.

## What each section does

### Identity

Reads eligible, effective rows from the instrument table for the exact requested ID. It retains source, effective dates, availability, and ingestion time. Competing rows are reported as ambiguous, not automatically resolved. The current alias table is not treated as a historical security master. Cross-dataset identifiers supplied in the same request remain caller assertions; their inclusion does not prove they represent the same historical security.

### Market

Includes completed daily Yahoo bars from observed `snapshot-v2` versions only. Each record retains OHLCV, event time, availability, ingestion time, source, and data version. Legacy `adjusted-v1` backfills are excluded. Even reconstructed mode cannot move a newly observed adjusted snapshot back to the original bar date.

Prices must be finite, positive, and consistent with OHLC bounds; volume must be finite and nonnegative. Missing data is never forward-filled or backward-filled. Daily bars are research context, not executable bid/ask quotes or evidence of a trading venue's current state.

### Fundamentals

Uses the explicitly supplied fundamental instrument ID and the latest eligible filing for each metric, reporting period, unit, and source. Quarter-only and year-to-date facts remain distinct. Filing accession, filing time, availability convention, local ingestion time, and original missing values are retained. No current Yahoo company profile is used to fill historical gaps.

### Macro

Provides a separate section for each explicit series ID and selects the latest eligible vintage of each observation. Source and vintage remain visible; the provider's mutable `realtime_end` is deliberately excluded from the packet rather than used to leak knowledge of a later revision.

Warehouse v4 does not store authoritative series units or frequency. This release therefore does not guess them, calculate yield spreads, pool different sources, or derive macro signals. A versioned series-metadata registry is a separate next step.

### News

Matches an exact entity in the stored JSON symbol array. For example, `JPM` is not a substring match for `JPMX`. Publication, first observation, and availability must all precede the cutoff. Records retain event IDs, source, title hash, entity IDs, and quality metadata; raw URLs are omitted.

Article bodies and headline text are not in the warehouse dataset used here. News is always marked as metadata-only, never interpreted as sentiment. No eligible records means no eligible records **in this warehouse snapshot**, not that no relevant news existed.

## Status and provenance

Sections use `AVAILABLE`, `PARTIAL`, `MISSING`, or `NOT_REQUESTED`. `AVAILABLE` only means eligible rows were found; it is not a completeness, freshness, identity-verification, model-admission, or trade-readiness verdict. Truncation and null values are explicit. The age field measures time since record availability, not quote freshness or time since the underlying economic event.

Every selected record has an evidence ID derived from its normalized content. The stable `snapshot_hash` covers the request and selected sections. Rebuilding unchanged evidence at a different generation time preserves this hash; `packet_hash` also covers generation time and the packet's safety flags and limitations.

## New code modules

| Module | Responsibility |
| --- | --- |
| `src/quantpaper/research/evidence_store.py` | Opens an existing schema-v4 DuckDB in read-only mode, disables external access/extension loading, and reads all sections in one transaction. It does not initialize or migrate a database. |
| `src/quantpaper/research/evidence.py` | Validates the request and records, computes deterministic IDs/hashes, reports coverage, and renders an English report. It contains no provider or order integration. |
| `src/quantpaper/research/evidence_cli.py` | Builds isolated create-only output and verifies archived runs. Help and archived verification do not import a broker, load credentials, or open DuckDB. |
| `.github/workflows/ci.yml` | Runs credential-free synthetic/mocked tests on Python 3.11 and 3.14, scans publication candidates and the full Git index, and smoke-tests the launcher. |

Each completed run contains:

- `packet.json`: Structured research evidence and explicit limitations.
- `report.md`: A readable English summary; only the first five selected records per section are displayed.
- `manifest.json`: Builder-source fingerprints, Python/DuckDB versions, request hash, and evidence hashes. This identifies the selected-row snapshot, not a full database backup or a dependency lockfile.
- `completion.json`: Written last; binds the exact output filenames and file hashes. Incomplete or altered runs fail verification.

All output is confined to a new child of `artifacts/research-evidence/`, which remains Git-ignored. Symlinked output paths and path traversal are rejected. No data, reports containing local data, database, model, or API credentials are published with source changes. Hashes detect ordinary modification; without an external trusted anchor, they do not prevent someone from rewriting an entire archive and its hashes.

## Local verification on 2026-09-07

All 431 project tests passed on the existing Python 3.14 environment, including 67 evidence-layer tests. The evidence tests use synthetic records, mocked snapshots, temporary databases, and subprocess import/network guards; they do not call a broker or provider.

An offline JPM packet was also built from the existing local warehouse at `2026-09-07T19:21:27.304008+00:00` and its archived hashes were verified. It contained one eligible identity row and a truncated sample of 100 news metadata records. Eligible observed market snapshots, fundamentals, and the four requested macro series were missing. Those gaps remained visible; no data was fetched or backfilled to hide them. The database's SHA-256 was unchanged by the read-only check. Generated data artifacts remain local and Git-ignored.

## What this does not establish

This release does not improve or retrain the prediction model by itself. It does not establish profitability, add historical news text, certify a survivorship-free universe, or turn daily research into HFT. Existing model approvals, shadow journals, order adapters, schedules, and trading gates remain unchanged.

The next research comparison should separate three effects: the existing baseline, the baseline with additional verified data, and the same data with an LLM interpretation layer. Use frozen time windows and budgets, record model/prompt versions, and measure incremental out-of-sample value alongside cost, latency, and invalid-output rates before considering any signal integration.
