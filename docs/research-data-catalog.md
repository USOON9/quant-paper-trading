# Observed Research Data Catalog

This step audits overlapping source identity assertions and records current FRED series definitions. It creates an independent, versioned catalog under `artifacts/research-catalog/`. It does not migrate or write the research warehouse, delete identity records, alter existing evidence packets, train a model, generate a trading feature, or contact a broker.

## Why this layer exists

Repeated Yahoo downloads with different start dates can leave multiple effective identity assertions. Agreement between those assertions is useful, but deleting one or inventing a combined historical validity interval would erase provenance. The catalog instead groups exact descriptions and retains every original assertion, timestamp, and hash.

Macro observations need explicit definitions before any numerical use. A price index, an interest rate expressed in percent, and a monthly unemployment rate are not interchangeable. A series' frequency also does not establish its release timestamp or a trading calendar. The catalog obtains definitions from the [official FRED series metadata endpoint](https://fred.stlouisfed.org/docs/api/fred/series.html), preserving full and abbreviated units, frequency, seasonal adjustment, the provider update timestamp, and the source URL.

The initial requested series are:

| Series | Meaning and current documented unit | Frequency / seasonal adjustment | Official definition |
| --- | --- | --- | --- |
| DFF | Effective federal funds rate; percent | Daily, 7-Day / not seasonally adjusted | [DFF](https://fred.stlouisfed.org/series/DFF) |
| DGS10 | 10-year constant-maturity Treasury yield; percent | Daily / not seasonally adjusted | [DGS10](https://fred.stlouisfed.org/series/DGS10) |
| CPIAUCSL | Consumer price index; Index 1982-1984=100, not an inflation percentage | Monthly / seasonally adjusted | [CPIAUCSL](https://fred.stlouisfed.org/series/CPIAUCSL) |
| UNRATE | Unemployment rate; percent | Monthly / seasonally adjusted | [UNRATE](https://fred.stlouisfed.org/series/UNRATE) |

This table documents the initial scope; the implementation fetches the exact provider description rather than assigning units from this table. FRED is the metadata distributor recorded by this connector, not a claim that FRED originally produces every underlying economic statistic. No unit conversion, inflation calculation, interpolation, or daily filling of monthly data occurs here.

## Run and inspect

The project's `data` dependency extra supplies the runtime. Keep `FRED_API_KEY` only in the local `.env` or environment, never in a command, report, commit, or chat message. The SEC contact and Alpaca credentials are not used for these metadata requests.

```bash
.venv/bin/python main.py catalog build \
  --db data/research.duckdb \
  --instrument-id YF:JPM \
  --series DFF DGS10 CPIAUCSL UNRATE \
  --run-dir artifacts/research-catalog/jpm-example-001

# Offline archive verification: no database, credentials, or provider request.
.venv/bin/python main.py catalog show \
  --run-dir artifacts/research-catalog/jpm-example-001

# This earlier cutoff must not see newly collected metadata.
.venv/bin/python main.py catalog check-time \
  --run-dir artifacts/research-catalog/jpm-example-001 \
  --as-of 2026-09-01T12:00:00Z
```

After package installation, `quant-catalog` offers the same commands. Each build accepts one explicit instrument ID and one to four unique uppercase series IDs. The existing database must be a schema-v4 `.duckdb` file under project `data/`. Database and output paths reject traversal and symlink components. Every attempt uses a new output directory; existing and interrupted archives cannot be overwritten.

`build` returns `0` when all requested metadata and a complete single-description identity audit are available. It returns `3` for an archived `PARTIAL` result. `show` returns `0` for a valid archive, including a partial one. `check-time` returns `3` if any requested metadata is unavailable at the cutoff; it returns records only when strictly eligible. Invalid inputs or verification failures return `2`. Read the JSON status: a complete catalog does not establish complete economic coverage or trading readiness.

## Identity: agreement without erasing history

The identity query reads one transaction with an explicit local-observed cutoff: `available_at < cutoff`, `ingested_at < cutoff`, and the effective interval contains the cutoff. It reads at most 501 rows. The 501st row is an overflow sentinel and is retained with the others; more than 500 eligible assertions makes resolution `TRUNCATED`, even if the returned descriptions agree.

| Resolution | Meaning |
| --- | --- |
| `SINGLE_ASSERTION` | One eligible provider assertion. This is not independent identity verification. |
| `EQUIVALENT_ASSERTIONS` | Multiple assertions match exactly on source, instrument ID, symbol, asset class, and primary exchange. |
| `AMBIGUOUS` | Sources or descriptors differ; all groups remain unresolved. |
| `MISSING` | No eligible assertion was found at this cutoff. |
| `TRUNCATED` | The bounded query cannot establish the complete assertion set. No identity is resolved. |

Null exchange values remain unknown. Cross-source matching is not inferred, and `YF:JPM` is not automatically merged with `US:JPM` or a SEC CIK. A matching current ticker cannot establish historical issuer continuity, ticker reuse, share-class continuity, or survivorship-free universe membership. Every resolution sets `historical_identity_verified` to false.

## Metadata has its own knowledge time

The identity cutoff is captured before the metadata requests. The catalog retains this cutoff separately from each metadata response's observation time and the catalog generation time. There is no claim that these independent sources were observed in one simultaneous transaction.

The connector requests exactly one current real-time day and one matching series from FRED. It rejects malformed or ambiguous responses, invalid field types, missing definitions, unexpected time bounds, provider updates after local observation, backwards clocks, and oversized JSON. It reuses bounded, fixed-host HTTPS transport with certificate and hostname verification, no redirects, no ambient proxies, and no automatic retries. Failed or unconfigured requests remain explicit; no alternate provider or stale cache is substituted.

`last_updated` is descriptive provider metadata. It is not the time this system learned the definition. `available_at` is exactly the local post-response `observed_at`. A request crossing UTC midnight retains its original request date and later observation timestamp without backdating either.

The offline time check requires both `record.available_at < decision_cutoff` and `catalog.generated_at < decision_cutoff`. Equality is unavailable. The generation timestamp denotes in-memory catalog assembly, not durable archive completion; archived readers additionally require a valid completion marker. Even a historical observation from an earlier year cannot use a newly downloaded definition in a local-observed historical experiment. The check does not reinterpret identity validity at a different cutoff, select macro observation vintages, calculate features, or validate a trading strategy. It proves temporal eligibility of these catalog records only, not freshness or semantic suitability for every future consumer.

## Archives and compatibility

Each archive contains:

| File | Purpose |
| --- | --- |
| `catalog.json` | Requested scope, retained raw identity assertions and grouping, validated metadata receipts, timestamps, safety flags, and a catalog hash. |
| `report.md` | Escaped, human-readable rendering of the validated catalog. |
| `manifest.json` | Builder-file hashes, runtime versions, catalog linkage, and manifest hash. |
| `completion.json` | Written last; binds the other three files by SHA-256. |

Metadata receipts preserve a normalized-record hash and a hash of the entire parsed provider response, including fields intentionally omitted from the catalog. Provider notes and raw responses are not retained; the original payload hash cannot be independently recomputed from the normalized record alone. Archive verification validates the stored record schema, request/series/time relationships, identity grouping, file hashes, and the rendered report, not an external provider signature. It cannot prevent coordinated rewriting of an archive and every associated hash without an independent trusted anchor.

All source collection finishes before a new archive is created. An interruption during fetching leaves no committed catalog and changes no warehouse rows. A filesystem failure after directory creation can leave an incomplete archive: preserve it and use a new directory for another attempt. A completion marker certifies a finished archive, not success of every requested source.

The previous evidence-v1 format and its immutable limitations are unchanged. Earlier packets still describe the schema-v4 warehouse they actually read, which does not contain this independent metadata catalog. This step neither retroactively enriches those packets nor suppresses their original warnings. A future combined evidence/feature artifact must explicitly bind the catalog hash, the evidence packet hash, matching instrument/series identifiers, and a valid decision cutoff; no implicit attachment exists today.

## Implementation map

| Module | Responsibility |
| --- | --- |
| `research/identity.py` | Pure assertion validation, deterministic grouping, and full reconstruction of archived resolution results. |
| `sources/fred_metadata.py` | Current FRED definitions with strict provider identity, date, text, payload, and local observation validation. |
| `research/catalog.py` | Read-only identity query, bounded metadata collection, create-only archives, semantic verification, and exclusive-cutoff checks. |
| `research/catalog_cli.py` | Explicit build, show, and check-time commands with sanitized failures and meaningful exit codes. |

The complete-code delivery discipline for this change includes runnable implementations, synthetic regression tests, archive verification, and preservation checks. Software correctness and richer metadata are not evidence of investment performance.

## Verified local outcome

The bounded local run on 2026-09-07 completed and its archive was independently verified. Its identity cutoff was `2026-09-07T20:52:15.800660+00:00`; catalog generation was `2026-09-07T20:52:17.275948+00:00`. Four successful FRED metadata responses supplied the requested definitions. Their local observation timestamps fall between those two bounds.

The two eligible `YF:JPM` assertions were retained in one exact same-source descriptor group, yielding `EQUIVALENT_ASSERTIONS`. Their distinct validity and knowledge timestamps remain visible in `catalog.json`; neither original row was deleted or rewritten. Primary exchange remains unknown, and historical identity remains unverified. All four metadata sections are `AVAILABLE`; the catalog's `COMPLETE` status applies to this bounded context audit, not to the model or historical data coverage.

The historical cutoff `2026-09-01T12:00:00Z` correctly returned no metadata records and exit code `3`. Additional offline boundary checks withheld every series one microsecond before and exactly at catalog generation, then admitted them one microsecond after generation. These checks establish the implemented strict timing convention, not a historical forward prediction.

All 567 local tests passed, including 21 identity tests, 19 FRED metadata tests, and 22 catalog integration tests. A before/after SHA-256 comparison confirmed all 62 protected files were byte-unchanged, including the entire research database, local `.env`, existing model inputs/artifacts, and previous evidence/refresh archives. Three earlier evidence archives and four refresh archives still passed their original verification routines. No training, orders, broker calls, gate changes, or scheduling changes occurred.

Only source code, tests, and English documentation are published. The catalog, prior artifacts, research data, and credentials remain local and Git-ignored.
