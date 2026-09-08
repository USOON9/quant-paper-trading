"""Narrow CLI for offline, research-only feature packets and archive checks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[3]
FAILURE_MESSAGE = (
    "Feature command failed. Check the existing research database, verified catalog, "
    "explicit instrument and timezone-aware cutoff, and isolated run directory. "
    "No exception payload is displayed."
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # Unknown arguments can contain accidental credentials or local paths.
        # Never echo parser-supplied values; --help lists the supported inputs.
        self.exit(2, "Invalid feature command. Use --help for supported arguments.\n")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="Offline research-only feature packets; no training or trading",
                     allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Build a bounded packet from an existing database and catalog",
                                allow_abbrev=False)
    build.add_argument("--db", type=Path, default=Path("data/research.duckdb"))
    build.add_argument("--catalog-run-dir", type=Path, required=True)
    build.add_argument("--instrument-id", default="YF:JPM")
    build.add_argument("--as-of", required=True, help="Exclusive ISO 8601 cutoff with an explicit timezone")
    build.add_argument("--run-dir", type=Path, required=True)
    show = commands.add_parser("show", help="Verify an archived packet without a database or provider",
                               allow_abbrev=False)
    show.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "build":
            from .evidence import utc_timestamp
            from .feature_packet import build_run, packet_summary

            as_of = utc_timestamp(args.as_of)
            packet = build_run(args.db, args.catalog_run_dir, args.run_dir,
                               project_root=PROJECT_ROOT, instrument_id=args.instrument_id, as_of=as_of)
        else:
            from .feature_packet import packet_summary, read_run

            packet = read_run(args.run_dir, project_root=PROJECT_ROOT)
        if not isinstance(packet, dict) or packet.get("status") not in {"COMPLETE", "PARTIAL"}:
            raise ValueError("unsupported feature packet status")
        exit_code = 3 if args.command == "build" and packet["status"] == "PARTIAL" else 0
        output = json.dumps(packet_summary(packet), indent=2, sort_keys=True, allow_nan=False)
        print(output)
        return exit_code
    except Exception:
        print(FAILURE_MESSAGE, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
