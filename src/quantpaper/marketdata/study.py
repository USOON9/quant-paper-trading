"""Create-only multi-session quote study; no trading or model interfaces."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from importlib.metadata import version
from pathlib import Path
import platform
import sys

from ..scheduler import require_paper_gates_closed
from .client import MarketDataClient, MarketDataError
from .storage import validate_run_directory, write_json
from .study_analytics import aggregate_observations, analyze_observation
from .study_protocol import build_study_plan


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SYMBOLS = ("SPY", "JPM", "XOM", "WMT", "JNJ", "BTC/USD")
REQUEST_INTERVAL_SECONDS = 0.4
_CIRCUIT_ERRORS = frozenset({
    "authentication", "permission", "rate_limit", "redirect", "configuration",
    "transport", "http", "malformed_response", "pagination", "validation",
})


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def source_snapshot() -> dict:
    """Snapshot an explicit source allowlist, never arbitrary files or secrets."""
    names = ["main.py", "pyproject.toml", "src/quantpaper/scheduler.py", "src/quantpaper/sessions.py"]
    names.extend(f"src/quantpaper/marketdata/{name}.py" for name in (
        "__init__", "client", "cli", "storage", "windows", "quality",
        "study_protocol", "study_analytics", "study",
    ))
    files = {}
    for name in names:
        path = PROJECT_ROOT / name
        if not path.resolve().is_relative_to(PROJECT_ROOT.resolve()):
            raise ValueError("source snapshot cannot leave the project")
        for node in (path, *path.parents):
            if node == PROJECT_ROOT:
                break
            if node.is_symlink():
                raise ValueError("source snapshot cannot follow symbolic links")
        raw = path.read_bytes()
        files[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "text": raw.decode("utf-8")}
    return {
        "files": files, "python": platform.python_version(),
        "libraries": {name: version(name) for name in (
            "requests", "numpy", "pandas", "exchange-calendars", "python-dotenv",
        )},
        "scope": "explicit collection/analysis source only; not an executable environment image",
    }


def _unavailable(symbol: str, kind: str, feed: str, start: datetime, end: datetime,
                 error: dict, *, attempted: bool) -> dict:
    return {
        "symbol": symbol, "kind": kind, "feed": feed,
        "start": start.isoformat(), "end": end.isoformat(),
        "observed_at": None, "failure_recorded_at": _stamp(),
        "attempted": attempted, "records": [], "pages": 0, "complete": False,
        "truncation_reason": "request_failed" if attempted else "circuit_breaker_not_requested",
        "content_hash": hashlib.sha256(b"[]").hexdigest(), "error": error,
        "observation_scope": "no usable response retained; failure time is not a market observation time",
    }


def capture_study(
    run_dir: Path, *, project_root: Path = PROJECT_ROOT, client=None,
    now: datetime | None = None, sessions: int = 5, stock_feed: str = "sip",
    symbols: tuple[str, ...] = SYMBOLS,
) -> dict:
    directory = validate_run_directory(run_dir, project_root)
    require_paper_gates_closed(project_root / ".env")
    plan = build_study_plan(now=now if now is not None else datetime.now(timezone.utc),
                            sessions=sessions, stock_feed=stock_feed, symbols=symbols)
    snapshot = source_snapshot()
    plan["collection"] = {
        "min_http_request_interval_seconds": REQUEST_INTERVAL_SECONDS,
        "pacing_scope": "one client/process only; other account users may consume the shared allowance",
        "http_scope": "GET data.alpaca.markets bars/quotes only",
        "on_request_error": "stop further requests; save all planned unrequested segments explicitly",
        "retries": 0, "silent_feed_fallback": False,
    }
    owned = client is None
    if owned:
        client = MarketDataClient.from_env(project_root / ".env",
                                          min_request_interval_seconds=REQUEST_INTERVAL_SECONDS)
    initial_request_count = getattr(client, "request_count", None)
    try:
        directory.mkdir(parents=True, exist_ok=False)
        artifacts = {"source.json": write_json(directory / "source.json", snapshot)}
        plan["source_snapshot_sha256"] = artifacts["source.json"]
        artifacts["plan.json"] = write_json(directory / "plan.json", plan)
        rows, errors = [], []
        circuit = None
        successful = attempted = skipped = retained_pages = raw_bars = raw_quotes = 0
        for observation in plan["observations"]:
            symbol, session, feed = (observation[key] for key in ("symbol", "session", "feed"))
            start, end = map(_parse, (observation["session_start"], observation["session_end"]))
            targets = {name: _parse(value) for name, value in observation["targets"].items()}
            legs = [("bars", "bars", start, end)] + [
                (name, "quotes", targets[name], targets[name] + timedelta(seconds=60))
                for name in ("opening", "midday", "closing")
            ]
            fetched = {}
            for leg, kind, request_start, request_end in legs:
                if circuit is not None:
                    skipped += 1
                    payload = _unavailable(symbol, kind, feed, request_start, request_end,
                                           {"category": "not_requested", "cause": circuit["category"]},
                                           attempted=False)
                else:
                    # Recheck before every segment, including injected offline clients.
                    require_paper_gates_closed(project_root / ".env")
                    attempted += 1
                    try:
                        payload = client.fetch(kind, symbol, request_start, request_end,
                                               feed=feed, max_pages=3)
                        successful += 1
                    except MarketDataError as error:
                        safe = {"symbol": symbol, "session": session, "leg": leg,
                                "category": error.category, "message": str(error)}
                        errors.append(safe)
                        payload = _unavailable(symbol, kind, feed, request_start, request_end,
                                               safe, attempted=True)
                        # Unknown client categories also fail closed, not retry indefinitely.
                        circuit = {**safe, "known_category": error.category in _CIRCUIT_ERRORS}
                filename = f"{symbol.replace('/', '-')}-{session}-{leg}.json"
                artifacts[filename] = write_json(directory / filename, payload)
                fetched[leg] = payload
                retained_pages += payload.get("pages", 0)
                if kind == "bars":
                    raw_bars += len(payload["records"])
                else:
                    raw_quotes += len(payload["records"])
            row = analyze_observation(
                symbol, fetched["bars"], {name: fetched[name] for name in targets},
                session_start=start, session_end=end, targets=targets,
            )
            rows.append(row)
            print(f"Study {symbol} {session}: saved 4 segments; "
                  f"requests={'stopped' if circuit else 'active'}", file=sys.stderr, flush=True)
        source_unchanged = source_snapshot() == snapshot
        aggregate = aggregate_observations(rows)
        final_request_count = getattr(client, "request_count", None)
        http_attempts = (final_request_count - initial_request_count
                         if type(initial_request_count) is int and type(final_request_count) is int else None)
        pagination_complete = all(row["complete"] for row in rows)
        report = {
            "contract": plan["contract"], "status": "captured" if not errors and pagination_complete else "partial",
            "status_scope": "transport/pagination only; inspect metadata and quality separately",
            "research_only": True, "execution_enabled": False, "model_updated": False,
            "executable_backtest": False, "strategy_validated": False,
            "started_at": plan["created_at"], "finished_at": _stamp(),
            "plan_sha256": artifacts["plan.json"], "source_snapshot_sha256": artifacts["source.json"],
            "source_unchanged_during_capture": source_unchanged,
            "access_scope": "historical requests only; realtime SIP entitlement was not tested",
            "counts": {
                "planned_observations": len(plan["observations"]),
                "planned_request_segments": plan["summary"]["expected_request_segments"],
                "attempted_request_segments": attempted, "successful_request_segments": successful,
                "skipped_request_segments": skipped, "http_attempts": http_attempts,
                "retained_response_pages": retained_pages,
                "raw_bars": raw_bars, "raw_quotes": raw_quotes,
            },
            "sample_days_by_asset": plan["summary"]["sample_days_by_asset"],
            "errors": errors, "circuit_breaker": circuit,
            "aggregate": aggregate, "observations": rows,
            "limitations": [
                "Only five sessions by default; recent consecutive days are not independent or representative of all regimes.",
                "Historical event timestamps do not establish local receive times, decision availability or execution latency.",
                "Latency scenarios select later historical quotes, not executable orders or fills; no strategy PnL is calculated.",
                "Quote conditions, trade status, actual fees, queue priority, market impact and partial fills remain unverified.",
                "Aggregation is within symbol/window with equal day weights, not pooled quotes across assets or days.",
                "Missing and skipped data remain visible; do not infer complete coverage from successful pagination.",
                "No model, shadow database, broker account, orders, positions or automation is changed.",
            ],
        }
        if not source_unchanged:
            report["status"] = "source_changed"
        artifacts["report.json"] = write_json(directory / "report.json", report)
        write_json(directory / "completion.json", {"status": "complete", "artifacts": artifacts})
        return report
    finally:
        if owned:
            client.close()


def study_summary(report: dict) -> dict:
    result = {key: report[key] for key in (
        "contract", "status", "status_scope", "execution_enabled", "model_updated",
        "source_unchanged_during_capture", "access_scope", "counts", "sample_days_by_asset",
        "errors", "circuit_breaker",
    )}
    result["symbols"] = {}
    for symbol, row in report["aggregate"]["by_symbol"].items():
        coverage = row["minute_coverage"]
        result["symbols"][symbol] = {
            "feed": row["feed"], "expected_sessions": row["expected_sessions"],
            "pagination_complete_sessions": row["pagination_complete_sessions"],
            "expected_minutes": coverage["expected_minutes"],
            "source_usable_valid_minutes": coverage["source_usable_valid_minutes"],
            "observed_missing_minutes": coverage["observed_missing_minutes"],
            "observed_zero_volume_minutes": coverage["observed_zero_volume_minutes"],
            "opening_closing_spread_pair": row["opening_closing_spread_pair"],
            "windows": {name: {key: window[key] for key in (
                "expected_samples", "usable_selected_samples", "usable_distribution_samples",
                "incomplete_or_truncated_window_count", "selected_full_spread_bps",
                "daily_window_event_median_spread_bps",
            )} for name, window in row["by_window"].items()},
        }
    result["detail_scope"] = "full per-day evidence, latency scenarios and quality reasons are in report.json"
    return result
