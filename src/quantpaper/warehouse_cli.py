"""Commands for initializing and inspecting the point-in-time warehouse."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .warehouse import PointInTimeWarehouse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Point-in-time research warehouse")
    parser.add_argument("command", choices=["init", "ingest-yahoo", "stats"])
    parser.add_argument("--db", type=Path, default=Path("data/research.duckdb"))
    parser.add_argument("--data-dir", type=Path, default=Path("data/yahoo"))
    args = parser.parse_args(argv)

    warehouse = PointInTimeWarehouse(args.db)
    try:
        warehouse.initialize()
        inserted: dict[str, int] = {}
        if args.command == "ingest-yahoo":
            for path in sorted(args.data_dir.glob("*.csv")):
                symbol = path.stem.replace("INDEX_", "^")
                asset_class = "crypto" if symbol.endswith("-USD") else "equity"
                inserted[symbol] = warehouse.ingest_yahoo_csv(symbol, path, asset_class)
        output = {"database": str(args.db), "inserted": inserted, "stats": asdict(warehouse.stats())}
        print(json.dumps(output, indent=2))
    finally:
        warehouse.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

