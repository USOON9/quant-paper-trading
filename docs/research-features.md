# As-of research features

## Purpose and boundaries

The `observed_context_v1` pipeline computes eleven deterministic research-context
features from the existing version-4 research warehouse and a verified observed
data catalog. It does not download data, read `.env`, train or deserialize models,
produce predictions, change admission thresholds, call a broker, or modify trading
permissions or schedules. The warehouse, earlier archives, and existing model
inputs remain unchanged.

This is a separate contract, not an extension of the existing `daily_pit_v2`
model's input vector. A `COMPLETE` packet means all eleven calculations passed
this bounded contract; it does **not** mean the data is suitable for training,
that a model has improved, or that a strategy can trade. `training_ready`,
`prediction_generated`, and `historical_forward_sample` remain false.

## Time and identity rules

Every selected source row must satisfy both `available_at < as_of` and
`ingested_at < as_of`. The cutoff must be timezone-aware and is normalized to
UTC. A source timestamp equal to the cutoff is not eligible. Yahoo daily bars
must also have completed their conservative 24-hour interval by availability
and by the cutoff. The reader uses the existing bounded, read-only warehouse
transaction and its latest-eligible-vintage selection.

Macro metadata is usable only when its observation timestamp **and** its
catalog's generation timestamp are strictly before the cutoff. A FRED
`last_updated` field is not substituted for when this project observed metadata.
This distinction matters because both values and definitions can change over
time; FRED documents the difference between current and historical real-time
information in its [real-time period documentation](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html).
The archive retains the selected normalized vintage observations following the
existing [FRED observation contract](https://fred.stlouisfed.org/docs/api/fred/series_observations.html).

Market features additionally require matching, resolved source descriptions in
the current evidence and the earlier catalog. Only Yahoo equity descriptions
whose instrument ID is `YF:` followed by their symbol are supported here.
Ambiguous or truncated identity evidence, an unavailable catalog, conflicting
descriptions, and unsupported assets block all six market values with
`IDENTITY_BLOCKED`. Their pre-mask quality statuses remain in
`unmasked_market_quality`. Exact same-source assertions are not a historical
security master: `historical_identity_verified` remains false.

The feature computation timestamp cannot precede its cutoff or either input
packet's generation. A packet assembled today for an old cutoff is explicitly
an as-of reconstruction, not proof that these features or a prediction were
actually emitted at that earlier time. Historical prices downloaded yesterday
do not become hundreds of historical local-observed training decision rows.

## Eleven feature slots

Here `C`, `O`, `H`, `L`, and `V` are the selected adjusted Yahoo close, open,
high, low, and volume. Index `-1` denotes the latest observed bar. Horizons are
observed bars, **not verified consecutive exchange sessions**.

| Feature | Calculation | Required inputs | Unit |
| --- | --- | --- | --- |
| `market.return_1_observed_bar` | `C[-1] / C[-2] - 1` | 2 bars | Fraction |
| `market.return_5_observed_bars` | `C[-1] / C[-6] - 1` | 6 bars | Fraction |
| `market.return_20_observed_bars` | `C[-1] / C[-21] - 1` | 21 bars | Fraction |
| `market.volatility_20_observed_intervals` | Sample standard deviation of the latest 20 simple close-to-close returns, `ddof=1` | 21 bars | Fraction, not annualized |
| `market.range_fraction` | `(H[-1] - L[-1]) / O[-1]` | 1 bar | Fraction |
| `market.volume_to_mean_20_observed_bars` | `V[-1] / mean(V[-20:])`; mean includes latest bar | 20 bars | Ratio |
| `macro.DFF.level` | Latest eligible observation-date value | DFF plus matching metadata | Percent |
| `macro.DGS10.level` | Latest eligible observation-date value | DGS10 plus matching metadata | Percent |
| `macro.CPIAUCSL.level` | Latest eligible observation-date value | CPIAUCSL plus matching metadata | Index, 1982–1984=100 |
| `macro.UNRATE.level` | Latest eligible observation-date value | UNRATE plus matching metadata | Percent |
| `macro.DGS10_minus_DFF.spread_pp` | DGS10 minus DFF, only when both latest observation dates are exactly equal | Two available rate levels | Percentage points |

Percent levels retain the provider's scale; `3.5` is not converted to `0.035`.
CPIAUCSL is a price-index level, not inflation or a year-over-year percentage.
The spread is a long-rate/overnight-rate context measure, not a 10-year/2-year
yield-curve spread. No older common-date pair is substituted if the latest rate
dates differ.

Metadata must match the complete frozen definitions: DFF is Daily, 7-Day / Not
Seasonally Adjusted; DGS10 is Daily / Not Seasonally Adjusted; CPIAUCSL and
UNRATE are Monthly / Seasonally Adjusted, with the units above. A definition
mismatch blocks the value instead of silently converting it.

## Missingness and fixed quality rules

The contract records formulas, units, status reasons, gate precedence, and the
following inclusive limits. These are explicit research assumptions, not
calibrated execution-safety limits or exchange-calendar guarantees.

| Check | Maximum accepted age or gap |
| --- | --- |
| Latest market event | 7 UTC calendar days before the cutoff date |
| Consecutive observed dates within each feature's required window | 7 calendar days |
| DFF / DGS10 latest observation availability | 7 elapsed days |
| CPIAUCSL / UNRATE latest observation availability | 62 elapsed days |
| DFF / DGS10 latest economic observation date | 7 UTC calendar days |
| CPIAUCSL / UNRATE latest economic observation date | 100 UTC calendar days |
| Macro metadata observation | 30 elapsed days |

Availability age and economic observation-date age are checked separately: a
new revision of an ancient observation must not count as current context. The
monthly 100-day allowance recognizes that monthly observations can be dated to
the first day of their reference month; it is not an inferred release calendar.
The independent 62-day availability limit still applies.

Absent metadata, insufficient bars, stale observations, excessive gaps, a null
latest macro value, mismatched rate dates, and invalid denominators remain
explicit statuses with `value: null`. In particular, a null latest macro value
does not fall back to an older non-null value. There is no forward fill,
interpolation, resampling, zero imputation, scaling, or model-ready dense vector.

Malformed rows, conflicting observation dates, invalid evidence IDs, non-finite
numbers, booleans in numeric fields, and future/equal-cutoff source timestamps
are rejected rather than silently discarded. At most 500 records per evidence
section are retained. Older-history truncation is visible and can coexist with
a valid recent 21-bar window; truncated identity evidence cannot authorize
market feature binding.

## Commands and archive verification

From the project directory, inspect `python main.py features build --help`.
For another one-time run, choose a timezone-aware cutoff no later than the
current time and a **new** output directory:

```bash
python main.py features build \
  --db data/research.duckdb \
  --catalog-run-dir artifacts/research-catalog/jpm-20260907-001 \
  --instrument-id YF:JPM \
  --as-of 2026-09-08T19:50:00Z \
  --run-dir artifacts/research-features/jpm-context-new-run

python main.py features show \
  --run-dir artifacts/research-features/jpm-context-new-run
```

The example requires the existing local warehouse and catalog; they are not
included in a GitHub clone. After installing the updated package, `quant-features`
offers the same subcommands. `build` returns 0 for `COMPLETE`, 3 for an archived
`PARTIAL` result, and 2 for an error. `show` returns 0 for either valid status and
2 if verification fails. Error output is sanitized; it does not echo exception
payloads or unknown argument values.

Each create-only directory under `artifacts/research-features/` contains:

| File | Responsibility |
| --- | --- |
| `features.json` | Eleven results, full contract, identity binding, coverage, input links, and hashes |
| `evidence.json` | Complete bounded source evidence used for this computation |
| `catalog.json` | Copied validated catalog, including records that were ineligible at the cutoff |
| `report.md` | Deterministically rendered English report |
| `manifest.json` | Relevant builder-file fingerprints, runtime versions, and packet/input hash links |
| `completion.json` | Written last; binds the five preceding file hashes |

Every feature retains source evidence IDs, selected metadata record hashes, and
the latest selected input availability. Market-window provenance includes all
rows checked for gaps, not only return endpoints. That per-feature availability
field is **not** the whole packet's known-at or emission time: catalog eligibility,
identity evidence, ingestion cutoffs, and actual computation time are checked
and recorded separately.

`show` requires no warehouse, original catalog directory, credentials, or network.
It validates copied inputs, recomputes every value and status under the frozen
contract, checks linked hashes, and re-renders the report. Merely recalculating
a checksum after editing a feature value does not pass recomputation. Hashes
are not provider signatures and cannot authenticate a coordinated rewrite of
all inputs and outputs without an external trusted anchor. Raw HTTP payloads
and the full database are not copied.

## Code sections and tests

- `research/feature_math.py`: pure validation, formulas, frozen quality policy,
  explicit missingness, and per-feature provenance.
- `research/feature_packet.py`: read-only orchestration, time/identity/catalog
  binding, copied input archives, deterministic reports, and offline recomputation.
- `research/feature_cli.py`: narrow `build` / `show` interface and sanitized errors.
- `tests/test_feature_math.py`: formula, numeric, boundary, missingness, metadata,
  stale-reference-period, and source-provenance cases.
- `tests/test_feature_packet.py`: temporary-database integration, old-cutoff
  exclusion, preservation, create-only path checks, and tamper/replay cases.
- `tests/test_feature_cli.py`: parser, dispatch, help, status codes, and error handling.

SEC accounting facts and news are intentionally not converted into features in
this contract. Cross-source issuer identity, accounting-period normalization,
filing acceptance timing, historical text rights, and publication/revision
semantics need their own tested definitions. Future training also needs
predeclared decision rows, labels with availability times, chronological
evaluation, cost stress, and an independent held-out comparison. This step
supplies auditable context inputs, not evidence of predictive improvement.

## Local verification on 2026-09-08

Two isolated runs reused the existing warehouse and
`artifacts/research-catalog/jpm-20260907-001`; neither fetched providers:

| Local run directory under `artifacts/research-features/` | Exclusive cutoff (UTC) | Result |
| --- | --- | --- |
| `jpm-local-observed-20260908-001` | 2026-09-08 20:05:04 | `COMPLETE`, 11/11 available |
| `jpm-pre-observation-20260908-001` | 2026-09-01 12:00:00 | `PARTIAL`, 0/11 available |

The current-cutoff packet was computed at 20:05:27 UTC. Its latest market bar
was September 4; the rate observations were September 3, CPI July 1, and
unemployment August 1. DFF was 3.63 percent, DGS10 4.77 percent, CPI 332.813
index points, unemployment 4.1 percent, and the same-date rate spread
approximately 1.14 percentage points. These are retained research observations,
not a claim about the latest live values or investment recommendations.

Five hundred market bars, 500 DFF observations, and 500 DGS10 observations were
retained with older coverage explicitly truncated. CPI retained 22 observations
and unemployment 23. Inherited evidence sections remain `PARTIAL` because their
original coverage and source limitations remain true; passing eleven local
calculations does not upgrade those source-quality judgments.

The September 1 negative control correctly excluded later-observed inputs and
catalog metadata. Both archives passed offline recomputation. All 635 project
tests passed, including 68 feature-math, packet, and CLI tests. A byte-hash
comparison confirmed all 66 protected files unchanged, including `.env`, the
whole warehouse, cached model inputs, model files, and earlier research
archives. All 3 earlier evidence, 4 refresh, and 1 catalog archives also passed
their unchanged readers. This workflow submitted no orders and did not alter
models or automation. Only source, tests, and English documentation are intended
for publication; feature archives remain local and Git-ignored.

```bash
python main.py features show --run-dir artifacts/research-features/jpm-local-observed-20260908-001
python main.py features show --run-dir artifacts/research-features/jpm-pre-observation-20260908-001
```
