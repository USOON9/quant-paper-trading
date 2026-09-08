"""Safe command-line interface for Alpaca paper account operations."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from alpaca.common.exceptions import APIError
from requests.exceptions import RequestException


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Invalid Alpaca command. Use --help; argument values are not echoed.\n")


def _amount(value):
    try:
        return Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("invalid amount") from None


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="Alpaca paper-only gateway", allow_abbrev=False)
    parser.add_argument("command", choices=["status", "quote", "preflight", "rehearse", "round-trip"])
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--notional", type=_amount, default=Decimal("5"))
    parser.add_argument("--run-id", help="Required unique identifier for one authorized round-trip attempt")
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--audit", type=Path, default=Path("artifacts/alpaca-paper-audit.jsonl"))
    args = parser.parse_args(argv)

    try:
        if args.command == "rehearse":
            from .paper_rehearsal import rehearse

            result = rehearse()
        else:
            from .alpaca_paper import AlpacaPaperService, PaperCredentials

            if args.command == "round-trip":
                if args.run_id is None:
                    raise ValueError("an explicit pilot run ID is required")
                AlpacaPaperService._pilot_request(args.symbol, args.notional, args.run_id)
            service = AlpacaPaperService(PaperCredentials.load(args.env), args.audit,
                                         read_only=args.command != "round-trip")
            if args.command == "status":
                result = service.status()
            elif args.command == "quote":
                result = asdict(service.latest_stock_quote(args.symbol))
            elif args.command == "preflight":
                result = service.preflight_stock(args.symbol, args.notional)
            else:
                result = service.round_trip_stock(args.symbol, args.notional, run_id=args.run_id)
    except (RuntimeError, ValueError, TimeoutError, OSError, TypeError, AttributeError):
        print("Alpaca paper command blocked or failed. Check preflight and local reconciliation state; no exception payload is displayed.", file=sys.stderr)
        return 2
    except (APIError, RequestException) as error:
        print(f"Alpaca paper request failed ({type(error).__name__}); inspect the audit and reconcile order state before retrying.", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 3 if args.command == "preflight" and result["status"] != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
