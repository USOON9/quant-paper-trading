"""CLI for audited point-in-time external data ingestion."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from dotenv import load_dotenv

from .alpaca_paper import PaperCredentials
from .sources.alpaca_news import AlpacaNewsSource
from .sources.fred import FREDVintageClient
from .sources.sec import SECCompanyFactsClient
from .warehouse import PointInTimeWarehouse


DEFAULT_FRED_SERIES = ["DFF", "DGS10", "CPIAUCSL", "UNRATE", "GDPC1", "VIXCLS"]


def _record_batch(warehouse: PointInTimeWarehouse, batch: object, inserted: int) -> dict[str, object]:
    run_id = warehouse.record_ingestion(
        batch.source, batch.request, len(batch.records), batch.content_hash
    )
    return {
        "source": batch.source,
        "fetched": len(batch.records),
        "inserted": inserted,
        "content_hash": batch.content_hash,
        "run_id": run_id,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Point-in-time external data ingestion")
    parser.add_argument("command", choices=["status", "report", "sec", "fred", "news"])
    parser.add_argument("--database", type=Path, default=Path("data/research.duckdb"))
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--ticker", default="JPM")
    parser.add_argument("--series", nargs="+", default=DEFAULT_FRED_SERIES)
    parser.add_argument("--symbols", nargs="+", default=["SPY", "JPM"])
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--report", type=Path, default=Path("artifacts/source-coverage.json"))
    args = parser.parse_args(argv)
    load_dotenv(args.env, override=False)
    warehouse = PointInTimeWarehouse(args.database)
    try:
        if args.command == "status":
            output = {
                "configuration": {
                    "sec_user_agent": bool(os.environ.get("SEC_USER_AGENT", "").strip()),
                    "fred_api_key": bool(os.environ.get("FRED_API_KEY", "").strip()),
                    "alpaca_credentials": bool(os.environ.get("APCA_API_KEY_ID", "").strip())
                    and bool(os.environ.get("APCA_API_SECRET_KEY", "").strip()),
                },
                "warehouse": asdict(warehouse.stats()),
            }
        elif args.command == "report":
            output = warehouse.quality_report()
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(output, indent=2, default=str) + "\n")
            output = {"report_path": str(args.report), **output}
        elif args.command == "sec":
            client = SECCompanyFactsClient(os.environ.get("SEC_USER_AGENT", ""))
            batch = client.fetch(args.ticker)
            available_at = datetime.now(timezone.utc).isoformat()
            with warehouse.transaction():
                warehouse.upsert_instrument_alias(
                    "sec-cik", str(batch.request["cik"]), f"US:{args.ticker.upper()}", available_at
                )
                warehouse.upsert_instrument_alias(
                    "yahoo", args.ticker.upper(), f"US:{args.ticker.upper()}", available_at
                )
                output = _record_batch(warehouse, batch, warehouse.ingest_fundamentals(batch.records))
        elif args.command == "fred":
            client = FREDVintageClient(os.environ.get("FRED_API_KEY", ""))
            results = []
            for series_id in args.series:
                batch = client.fetch(series_id)
                with warehouse.transaction():
                    inserted = warehouse.ingest_macro(batch.records)
                    result = _record_batch(warehouse, batch, inserted)
                results.append(result)
            output = {"series": results}
        else:
            batch = AlpacaNewsSource(PaperCredentials.load(args.env)).fetch(
                args.symbols, args.days, args.limit
            )
            with warehouse.transaction():
                output = _record_batch(warehouse, batch, warehouse.ingest_news(batch.records))
        print(json.dumps(output, indent=2, default=str))
        return 0
    except (RuntimeError, ValueError, KeyError) as error:
        print(f"Source ingestion error: {error}", file=sys.stderr)
        return 2
    finally:
        warehouse.close()


if __name__ == "__main__":
    raise SystemExit(main())
