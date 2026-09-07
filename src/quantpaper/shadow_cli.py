"""Locked, session-aware shadow cycle: no orders and no assumed future bars."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import tempfile

from .ml.regime import CONTEXT_SYMBOLS
from .ml.yahoo import YahooDailyData
from .shadow import ShadowJournal, generate_shadow_signals, load_shadow_model
from .scheduler import eligible_symbols, exclusive_cycle_lock, require_paper_gates_closed


DEFAULT_SYMBOLS = ["SPY", "JPM", "XOM", "WMT", "JNJ", "BTC-USD"]


def _download_frames(source: YahooDailyData, symbols: list[str], period: str):
    results = {symbol: source.download(symbol, period, include_current_completed=True)
               for symbol in symbols}
    return {symbol: source.load(result.path) for symbol, result in results.items()}


def _save_report(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, suffix=".tmp", delete=False) as f:
            temporary = Path(f.name)
            json.dump(value, f, indent=2, default=str, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def execute(args, journal: ShadowJournal) -> dict:
    if args.command == "report":
        return journal.report()
    require_paper_gates_closed(args.env)
    create = args.command in {"cycle", "run"}
    metadata = json.loads(args.metadata.read_text()) if create else None
    if create:
        load_shadow_model(args.model, metadata)  # Fail before download or ledger mutation.
    all_symbols = list(dict.fromkeys([
        *args.symbols, *journal.pending_symbols(), *(CONTEXT_SYMBOLS if create else []),
    ]))
    frames = _download_frames(YahooDailyData(args.data_dir), all_symbols, args.period)
    now = datetime.now(timezone.utc)
    eligible = eligible_symbols(args.symbols, now) if create else []
    skipped = {symbol: "crypto requires separately trained delayed-entry target"
               for symbol in args.symbols if symbol.endswith("-USD")}
    skipped.update({s: "no future session with completed fresh features in this time window"
                    for s in args.symbols if s not in eligible and s not in skipped})
    signals = []
    if create:
        context = {symbol: frames[symbol] for symbol in CONTEXT_SYMBOLS}
        for symbol in eligible:
            try:
                signals.extend(generate_shadow_signals(
                    args.model, metadata, {symbol: frames[symbol]}, context, args.cost_bps, now,
                ))
            except ValueError as error:
                skipped[symbol] = str(error)
    with journal.transaction():
        settled = journal.settle(frames, now) if args.command in {"cycle", "settle"} else 0
        inserted = journal.append(signals)
    return {
        "execution_enabled": False, "cycle_time": now.isoformat(),
        "eligible_symbols": eligible, "skipped": skipped,
        "settled": settled, "generated": len(signals), "inserted": inserted,
        # Newly computed candidates might be duplicates; journal is authoritative.
        "journal": journal.report(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Forward shadow journal with declared target sessions")
    parser.add_argument("command", choices=["run", "settle", "report", "cycle"])
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--period", default="2y")
    parser.add_argument("--cost-bps", type=float, default=5.0, help="total round-trip bps")
    parser.add_argument("--database", type=Path, default=Path("data/research.duckdb"))
    parser.add_argument("--data-dir", type=Path, default=Path("data/shadow/yahoo"))
    parser.add_argument("--model", type=Path, default=Path("artifacts/yahoo_walkforward_v2_model.joblib"))
    parser.add_argument("--metadata", type=Path, default=Path("artifacts/yahoo_walkforward_v2_model.json"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/shadow-report.json"))
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--lock", type=Path, default=None)
    args = parser.parse_args(argv)
    if not math.isfinite(args.cost_bps) or args.cost_bps < 0:
        parser.error("--cost-bps must be finite and nonnegative")
    args.symbols = list(dict.fromkeys(s.strip().upper() for s in args.symbols))
    # Every command locks before opening DuckDB, including manual run/settle/report.
    lock = args.lock or args.database.resolve().with_suffix(".shadow.lock")
    try:
        with exclusive_cycle_lock(lock):
            journal = ShadowJournal(args.database)
            try:
                output = execute(args, journal)
                output = {"report_path": str(args.output), **output}
                _save_report(args.output, output)
            finally:
                journal.close()
        print(json.dumps(output, indent=2, default=str, allow_nan=False))
        return 0
    except (RuntimeError, ValueError, OSError, KeyError) as error:
        # Failure does not overwrite the last successful report.
        print(f"Shadow mode error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
