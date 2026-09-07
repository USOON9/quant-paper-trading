"""Bounded historical market-data capture: GET-only, never order execution."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
from pathlib import Path
import json
import sys

from ..scheduler import require_paper_gates_closed
from .client import MarketDataClient, MarketDataError
from .quality import analyze_symbol
from .storage import read_report, validate_run_directory, write_json
from .windows import plan_windows, utc


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SYMBOLS = ("SPY", "JPM", "XOM", "WMT", "JNJ", "BTC/USD")


def capture(
    run_dir: Path, *, project_root: Path = PROJECT_ROOT, client=None,
    now: datetime | None = None, stock_session: str | None = None,
    crypto_session: str | None = None, stock_feed: str = "sip", symbols: tuple[str, ...] = SYMBOLS,
) -> dict:
    directory = validate_run_directory(run_dir, project_root)
    # This happens even for injected clients, before credentials or network calls.
    require_paper_gates_closed(project_root / ".env")
    if stock_feed not in {"sip", "iex"}:
        raise ValueError("stock feed must be explicitly sip or iex; no implicit fallback")
    if not symbols or len(symbols) != len(set(symbols)) or not set(symbols).issubset(SYMBOLS):
        raise ValueError("capture symbols must be a unique nonempty supported subset")
    started = utc(now if now is not None else datetime.now(timezone.utc))
    windows = plan_windows(now=started, stock_session=stock_session, crypto_session=crypto_session)
    client = MarketDataClient.from_env(project_root / ".env") if client is None else client
    directory.mkdir(parents=True, exist_ok=False)
    plan = {
        "contract": "intraday_quote_audit_v1", "created_at": started.isoformat(),
        "research_only": True, "execution_enabled": False,
        "stock_feed": stock_feed, "crypto_feed": "crypto_us", "symbols": list(symbols),
        "quote_window_seconds": 60, "max_quote_wait_seconds": 30, "max_pages_per_request": 3,
        "windows": {group: {key: value.isoformat() if isinstance(value, datetime) else value
                            for key, value in window.items()} for group, window in windows.items()},
        "label_scope": "execution-data diagnostic windows, NOT frozen v3 model labels or predictions",
        "http_scope": "GET data.alpaca.markets historical bars and quotes only",
        "libraries": {name: version(name) for name in ("requests", "pandas", "exchange-calendars")},
    }
    artifacts = {"plan.json": write_json(directory / "plan.json", plan)}
    errors = []
    summaries = {}
    successful_responses = 0
    for symbol in symbols:
        window = windows["crypto" if symbol == "BTC/USD" else "stocks"]
        feed = "crypto_us" if symbol == "BTC/USD" else stock_feed
        fetched = {}
        for leg, kind, start, end in (
            ("bars", "bars", window["session_start"], window["session_end"]),
            ("entry_quotes", "quotes", window["entry_at"], window["entry_at"] + timedelta(seconds=60)),
            ("exit_quotes", "quotes", window["exit_at"], window["exit_at"] + timedelta(seconds=60)),
        ):
            try:
                payload = client.fetch(kind, symbol, start, end, feed=feed, max_pages=3)
                successful_responses += 1
            except MarketDataError as error:
                safe_error = {"symbol": symbol, "leg": leg, "category": error.category,
                              "message": str(error)}
                errors.append(safe_error)
                payload = {
                    "symbol": symbol, "kind": kind, "feed": feed,
                    "start": start.isoformat(), "end": end.isoformat(),
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "records": [], "pages": 0, "complete": False,
                    "truncation_reason": "request_failed", "error": safe_error,
                }
            fetched[leg] = payload
            filename = f"{symbol.replace('/', '-')}-{leg}.json"
            artifacts[filename] = write_json(directory / filename, payload)
        diagnostics = analyze_symbol(
            symbol, fetched["bars"], fetched["entry_quotes"], fetched["exit_quotes"],
            session_start=window["session_start"], session_end=window["session_end"],
            entry_at=window["entry_at"], exit_at=window["exit_at"], max_quote_wait_seconds=30,
        )
        summaries[symbol] = {"session": window["session"], **diagnostics}
        print(f"Captured {symbol}: bars={len(fetched['bars']['records'])}, "
              f"entry_quotes={len(fetched['entry_quotes']['records'])}, "
              f"exit_quotes={len(fetched['exit_quotes']['records'])}", file=sys.stderr, flush=True)
    report = {
        "contract": "intraday_quote_audit_v1", "research_only": True,
        "execution_enabled": False, "executable_backtest": False, "model_updated": False,
        "started_at": started.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
        "plan_sha256": artifacts["plan.json"],
        "status": "partial" if errors or not all(row["complete"] for row in summaries.values()) else "captured",
        "access": {"successful_responses": successful_responses,
                   "stock_feed_requested": stock_feed, "crypto_location": "us",
                   "scope": "historical request access only; realtime SIP entitlement was NOT tested"},
        "symbols": summaries, "errors": errors,
        "limitations": [
            "One completed session per asset is a connectivity/quality sample, not strategy validation or representative costs.",
            "Historical backfill first observed now is not evidence of what this process knew at historical decision time.",
            "First quote after the chosen time is only a touch-price diagnostic, not a guaranteed fill or latency simulation.",
            "IEX is exchange-only; SIP and Alpaca crypto US are distinct sources; no silent substitution or cross-feed pairing.",
            "Displayed quote sizes are raw vendor units and do not establish tradable capacity or fills.",
            "Spread diagnostics omit actual account fees, market impact, queue position, latency and partial fills.",
            "No fees were fetched from an account; fee scenarios if present are assumptions, not actual tariffs.",
            "No order, account or position endpoints are used. Existing v2/v3 models, shadow database and automation are unchanged.",
        ],
    }
    artifacts["report.json"] = write_json(directory / "report.json", report)
    write_json(directory / "completion.json", {"status": "complete", "artifacts": artifacts})
    return report


def summary(report: dict) -> dict:
    if report.get("contract") == "offline_intent_admission_v1":
        from ..admission_runner import admission_summary

        return admission_summary(report)
    if report.get("contract") == "intraday_asof_replay_v1":
        from .replay_runner import replay_summary

        return replay_summary(report)
    if report.get("contract") == "intraday_multi_session_v1":
        from .study import study_summary

        return study_summary(report)
    symbols = {}
    for symbol, row in report["symbols"].items():
        quality = row["bar_quality"]
        pair = row.get("cost_pair")
        symbols[symbol] = {
            "session": row["session"], "feed": row["feed"],
            "pagination_complete": row["complete"], "quality_reasons": row["reasons"],
            "minute_bars": {key: quality[key] for key in (
                "expected_minutes", "valid_count", "missing_count", "zero_volume_count", "invalid_count",
            )},
            "observed_round_trip_spread_bps": pair["approximate_round_trip_spread_bps"] if pair else None,
        }
    return {
        "execution_enabled": False, "model_updated": False, "status": report["status"],
        "access": report["access"], "errors": report["errors"],
        "status_scope": "transport/pagination only; inspect per-symbol quality reasons",
        "spread_scope": "selected quote-pair approximation, not fees, total cost, fills or strategy PnL",
        "symbols": symbols,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only minute/quote data audit; no account or order requests")
    parser.add_argument("command", choices=["capture", "study", "replay-capture", "replay", "admission", "show"])
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--stock-session")
    parser.add_argument("--crypto-session")
    parser.add_argument("--stock-feed", choices=["sip", "iex"], default="sip")
    parser.add_argument("--sessions", type=int, default=5,
                        help="study only: completed sessions per asset (1 to 10, default 5)")
    parser.add_argument("--source-dir", type=Path,
                        help="replay-capture: frozen study; replay/admission: frozen as-of quote capture")
    args = parser.parse_args(argv)
    try:
        if args.command == "admission":
            if args.source_dir is None or args.stock_session or args.crypto_session:
                raise ValueError("admission requires --source-dir and uses its frozen dates")
            from ..admission_runner import run_admission

            report = run_admission(args.run_dir, args.source_dir)
        elif args.command in {"replay", "replay-capture"}:
            if args.source_dir is None or args.stock_session or args.crypto_session:
                raise ValueError("replay requires --source-dir and uses its frozen dates")
            from .replay_runner import run_replay

            report = run_replay(args.run_dir, args.source_dir, collect=args.command == "replay-capture")
        elif args.command == "study":
            if args.stock_session or args.crypto_session:
                raise ValueError("study selects the latest completed sessions; explicit single dates are capture-only")
            from .study import capture_study

            report = capture_study(args.run_dir, sessions=args.sessions, stock_feed=args.stock_feed)
        elif args.command == "capture":
            report = capture(args.run_dir, stock_session=args.stock_session,
                             crypto_session=args.crypto_session, stock_feed=args.stock_feed)
        else:
            report = read_report(args.run_dir, PROJECT_ROOT)
        print(json.dumps({"report_path": str(args.run_dir / "report.json"), **summary(report)},
                         indent=2, allow_nan=False))
        return 0 if report["status"] in {"captured", "replayed", "audited"} else 2
    except (MarketDataError, ValueError, OSError, RuntimeError) as error:
        print(f"Market data audit error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
