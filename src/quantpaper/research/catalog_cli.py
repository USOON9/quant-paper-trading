"""Explicit current-context catalog collection and offline verification."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .catalog import DEFAULT_SERIES, build_catalog, metadata_as_of, read_catalog, summary


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only warehouse identity audit and observed macro metadata")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Read identity and fetch current FRED metadata; create a new catalog")
    build.add_argument("--db", type=Path, default=Path("data/research.duckdb"))
    build.add_argument("--instrument-id", default="YF:JPM")
    build.add_argument("--series", nargs="+", default=list(DEFAULT_SERIES))
    build.add_argument("--run-dir", type=Path, required=True)
    show = commands.add_parser("show", help="Verify catalog artifacts without database, credentials, or network")
    show.add_argument("--run-dir", type=Path, required=True)
    check = commands.add_parser("check-time", help="Offline metadata availability check at an exclusive cutoff")
    check.add_argument("--run-dir", type=Path, required=True)
    check.add_argument("--as-of", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            catalog = build_catalog(args.db, args.run_dir, project_root=PROJECT_ROOT,
                                    instrument_id=args.instrument_id, series=tuple(args.series))
        else:
            catalog = read_catalog(args.run_dir, project_root=PROJECT_ROOT)
        output = metadata_as_of(catalog, args.as_of) if args.command == "check-time" else summary(catalog)
        print(json.dumps(output, indent=2, sort_keys=True))
        if args.command == "build" and catalog["status"] != "COMPLETE":
            return 3
        if args.command == "check-time" and any(item["status"] != "AVAILABLE" for item in output["macro_metadata"].values()):
            return 3
        return 0
    except Exception:
        print("Research catalog failed. Check the existing warehouse, local FRED configuration, explicit scope, "
              "and a new isolated directory. No exception payload is displayed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
