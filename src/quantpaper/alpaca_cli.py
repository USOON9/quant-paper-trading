"""Safe command-line interface for Alpaca paper account operations."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from alpaca.common.exceptions import APIError
from requests.exceptions import RequestException

from .alpaca_paper import AlpacaPaperService, PaperCredentials


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Alpaca paper-only gateway")
    parser.add_argument("command", choices=["status", "quote", "submit-cancel", "round-trip"])
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--quantity", type=Decimal, default=Decimal("1"))
    parser.add_argument("--notional", type=Decimal, default=Decimal("5"))
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--audit", type=Path, default=Path("artifacts/alpaca-paper-audit.jsonl"))
    args = parser.parse_args(argv)

    try:
        service = AlpacaPaperService(PaperCredentials.load(args.env), args.audit)
        if args.command == "status":
            result = service.status()
        elif args.command == "quote":
            result = asdict(service.latest_stock_quote(args.symbol))
        elif args.command == "submit-cancel":
            result = service.submit_cancel_smoke(args.symbol, args.quantity)
        else:
            result = service.round_trip_stock(args.symbol, args.notional)
    except (RuntimeError, ValueError, TimeoutError) as error:
        print(f"Alpaca paper error: {error}", file=sys.stderr)
        return 2
    except (APIError, RequestException) as error:
        print(f"Alpaca paper request failed ({type(error).__name__}); inspect the audit and reconcile order state before retrying.", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
