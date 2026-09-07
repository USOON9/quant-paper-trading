# Offline Synthetic Order Intents: Admission and Audit

## Scope of This Stage

This stage connects the previous stage's quote-state reconstruction at target times to an independent order-intent admission layer, checking whether sufficient evidence exists to proceed with offline simulation. It does not generate actual model signals and is not an order executor; no Alpaca account, position, or order endpoints were called.

Each frozen target time generates synthetic BUY/SELL intents, evaluated at event-time offsets of 0, 250, and 1000 milliseconds. Every intent has a fixed notional of 5 dollars; this is a test input, not a recommended position size. The hard limits of 25 dollars per intent and 1000 milliseconds of quote age are research safety constraints, not calibrated trading parameters.

## Responsibilities of the New Sections

| File | Responsibility |
| --- | --- |
| `src/quantpaper/admission.py` | Admission functions without external side effects. Validate intents, notionals, provenance, model status, decision quotes, and arrival quotes; accumulate rejection reasons rather than skipping market-data checks when the model fails. |
| `src/quantpaper/admission_runner.py` | Offline runner. Verify the historical evidence index and frozen research protocol, reanalyze raw quotes, and compare each result; verify model-file hashes without loading or executing joblib; write only to a new audit directory. |
| `src/quantpaper/marketdata/cli.py` | Add the `admission` command and use the existing `show` command to view reports; the historical evidence directory must be supplied explicitly. |
| `tests/test_admission*.py` | Cover notional boundaries, temporal causality, source anomalies, model status, audit hash chains, input tampering, isolation, and command-line dispatch. |
| `scripts/check_publish_secrets.py` | Before GitHub publication, check allowed code paths, candidate files, and the entire Git index; compare local credentials in memory without exposing keys in reports. |
| `.gitignore` | Ignore credential files, data, models, audit artifacts, virtual environments, and caches; include filename patterns only, never the API key itself. |

## Lifecycle and Evidence

Each intent has only three logical events: `INTENT_CREATED` → `CHECKS_COMPLETED` → `REJECTED` or `APPROVED_FOR_SIMULATION_ONLY`. The latter state still does not permit order submission or authorize Paper or real-money trading.

Intent IDs bind the source completion hash, instrument, side, notional, decision time, and time offset. `events.json` links all logical events through consecutive sequence numbers and a SHA-256 hash chain. The log records the observation time of this run rather than presenting historical data read now as messages received at the historical time. This is locally verifiable audit evidence, not a digital signature or a broker execution report.

The output directory contains the plan, source snapshot, model status and hashes, per-intent results, revalidated quote states, event chain, summary report, and completion index. Existing models, market-data evidence, and databases remain unchanged. The outputs are not uploaded to GitHub.

## What the Model Status Means

Reading `evaluation.approved_for_paper_signals` from the currently frozen v2 model indicates only **the model's current admission status**. It must not be treated as approval, training results, or predictions that already existed at historical target times. This stage does not retrain, replace v2/v3, generate model trading signals, or calculate strategy PnL.

Even if all admission checks pass in the future, execution-layer evidence would still be missing for actual receive times, trading-halt status, accounts/inventory/margin, fees, queues, impact, order recovery, and broker reconciliation. Quote acceptance rates and synthetic-intent results must not be interpreted as fill rates or returns.

## Actual Results of This Run

On 2026-09-07, one offline check was performed against the complete `20260907-asof-offline-check` evidence: 90 windows, 540 unique intents, and 1,620 logical audit events. Every intent was `REJECTED` and included `MODEL_NOT_APPROVED`. There were no network requests, order submissions, simulated fills, or model updates. Hashes of models, databases, existing evidence, and automated tasks remained unchanged.

Of these intents, 386 were rejected solely because the model was not approved; 154 also had at least one quote issue. The following rejection reasons are counted by intent. An intent may have multiple reasons, so these counts must not be summed to obtain the total number of rejections:

| Reason | Intent count |
| --- | ---: |
| Model not approved for admission | 540 |
| Missing decision quote | 60 |
| Stale decision quote | 84 |
| Missing arrival quote | 44 |
| Stale arrival quote | 56 |
| Invalid or ambiguous arrival quote | 4 |

All 359 project tests passed, as did `pip check`. The current NumPy/pandas combination still emits the existing timedelta deprecation warnings; the runtime environment was not upgraded in this stage. Independent review added defensive checks preventing event-time regression between states and changes to the content of the same event.

## How to Run

Complete, hash-consistent as-of replay evidence and the frozen v2 model must already exist locally. GitHub contains source code only, not these local datasets or models.

```sh
# View this run's results without network access or order submission:
.venv/bin/python main.py marketdata show --run-dir artifacts/marketdata/20260907-intent-admission

# Use a new output directory for every subsequent run:
.venv/bin/python main.py marketdata admission --source-dir artifacts/marketdata/20260907-asof-offline-check --run-dir artifacts/marketdata/NEW-INTENT-AUDIT
```

Both Paper order gates must remain `NO`. If inputs or source code change during a run, the runner will not write a completion index.

See [github-security.md](github-security.md) for GitHub publication rules and checks.
