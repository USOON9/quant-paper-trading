"""Explicit research-data refresh; status/show do not contact a provider."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .refresh import SOURCES, load_configuration, read_refresh, run_refresh


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded, audited research-data refresh; no trading")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Check source configuration without opening a database or network")
    run = commands.add_parser("run", help="Fetch selected research sources and append validated data")
    run.add_argument("--db", type=Path, default=Path("data/research.duckdb"))
    run.add_argument("--run-dir", type=Path, required=True)
    run.add_argument("--symbol", default="JPM")
    run.add_argument("--asset-class", choices=["equity", "crypto"], default="equity")
    run.add_argument("--period", choices=["1y", "2y"], default="2y")
    run.add_argument("--sources", nargs="+", choices=SOURCES, default=list(SOURCES))
    run.add_argument("--series", nargs="*", default=["DFF", "DGS10", "CPIAUCSL", "UNRATE"])
    show = commands.add_parser("show", help="Verify archived refresh receipts without a provider or database")
    show.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            output = {"sources": load_configuration(PROJECT_ROOT).readiness(),
                      "research_only": True, "execution_enabled": False}
        elif args.command == "show":
            output = read_refresh(args.run_dir, PROJECT_ROOT)
        else:
            output = run_refresh(args.db, args.run_dir, project_root=PROJECT_ROOT,
                                 symbol=args.symbol, asset_class=args.asset_class, period=args.period,
                                 sources=tuple(args.sources), series=tuple(args.series))
        print(json.dumps(output, indent=2, sort_keys=True))
        # A verified PARTIAL archive is useful evidence, not a successful refresh
        # for a shell chain that would otherwise continue on a zero exit code.
        return 3 if args.command == "run" and output["status"] != "COMPLETE" else 0
    except Exception:
        print("Research refresh failed. Check local configuration, an existing schema-v4 database, "
              "and a new isolated run directory; no exception payload is displayed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
