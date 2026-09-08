"""Official Alpaca SDK adapter locked to paper trading.

No live-mode parameter or production URL is exposed. Trading methods require
explicit environment gates in addition to paper credentials.
"""

from __future__ import annotations

import os
import fcntl
import hashlib
import json
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import re
from typing import Any
from uuid import UUID

from alpaca.data.enums import DataFeed
from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, MarketOrderRequest
from dotenv import load_dotenv
from requests.exceptions import RequestException

from .audit import AuditTrail
from .paper_guard import PaperSessionGuard
from .paper_policy import evaluate_preflight
from .paper_transport import configure_transport


class ExactMarketOrderRequest(MarketOrderRequest):
    """Preserve decimal quantities through SDK validation and JSON serialization."""

    qty: Decimal | None = None
    notional: Decimal | None = None

    def to_request_fields(self) -> dict[str, Any]:
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in super().to_request_fields().items()
        }


class ExactLimitOrderRequest(LimitOrderRequest):
    qty: Decimal | None = None
    limit_price: Decimal | None = None

    def to_request_fields(self) -> dict[str, Any]:
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in super().to_request_fields().items()
        }


class PaperReconciliationRequired(RuntimeError):
    """Order/position state is unresolved; new operations must remain blocked."""


def _order_status(order: Any) -> str:
    return str(getattr(order.status, "value", order.status)).lower()


TERMINAL_STATUSES = {"filled", "canceled", "expired", "rejected"}
# Resolve against the installed project, never against --audit or the caller's
# current directory. All command invocations in this project share key state.
PAPER_STATE_DIRECTORY = Path(__file__).resolve().parents[2] / "data" / "paper-state"
PAPER_PILOT_DIRECTORY = Path(__file__).resolve().parents[2] / "data" / "paper-pilot"


@dataclass(frozen=True, slots=True)
class PaperCredentials:
    key_id: str
    secret_key: str

    @classmethod
    def load(cls, env_path: Path = Path(".env")) -> "PaperCredentials":
        load_dotenv(env_path, override=False)
        key = (
            os.environ.get("APCA_API_KEY_ID", "")
            or os.environ.get("ALPACA_API_KEY", "")
        ).strip()
        secret = (
            os.environ.get("APCA_API_SECRET_KEY", "")
            or os.environ.get("ALPACA_SECRET_KEY", "")
        ).strip()
        if not key or not secret:
            raise RuntimeError(
                "Alpaca paper credentials are missing. Put them in the local .env file; "
                "do not paste them into chat."
            )
        return cls(key, secret)


@dataclass(frozen=True, slots=True)
class PaperQuote:
    symbol: str
    timestamp: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float


class AlpacaPaperService:
    def __init__(self, credentials: PaperCredentials, audit_path: Path, *, read_only: bool = False) -> None:
        self._read_only = read_only
        self.audit = None if read_only else AuditTrail(audit_path)
        self.trading = TradingClient(credentials.key_id, credentials.secret_key, paper=True)
        self.stock_data = StockHistoricalDataClient(credentials.key_id, credentials.secret_key)
        configure_transport(self.trading, host="paper-api.alpaca.markets", read_only=read_only)
        configure_transport(self.stock_data, host="data.alpaca.markets", read_only=True)
        # One local lifecycle per paper API key, even with different audit paths.
        key_hash = hashlib.sha256(credentials.key_id.encode()).hexdigest()[:24]
        lock_directory = Path(tempfile.gettempdir()) / "quantpaper-order-locks"
        if not read_only:
            lock_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock_path = lock_directory / f"{key_hash}.lock"
        # Durable marker survives process death/reboot, unlike the ephemeral lock.
        # Its identity is the API key, not the user-selectable audit filename.
        if not read_only:
            PAPER_STATE_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._incident_path = PAPER_STATE_DIRECTORY / f"{key_hash}.reconciliation.json"
        self._legacy_incident_paths = (
            audit_path.resolve().with_suffix(".reconciliation.json"),
            PAPER_STATE_DIRECTORY.parents[1] / "artifacts" / "alpaca-paper-audit.reconciliation.json",
        )

    def status(self) -> dict[str, Any]:
        account = self.trading.get_account()
        clock = self.trading.get_clock()
        positions = self.trading.get_all_positions()
        orders = self.trading.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
        return {
            "account_id_suffix": str(account.id)[-6:],
            "status": str(account.status),
            "currency": account.currency,
            "cash": str(account.cash),
            "equity": str(account.equity),
            "buying_power": str(account.buying_power),
            "trading_blocked": bool(account.trading_blocked),
            "market_is_open": bool(clock.is_open),
            "next_open": str(clock.next_open),
            "next_close": str(clock.next_close),
            "open_orders": len(orders),
            "positions": [
                {
                    "symbol": position.symbol,
                    "qty": str(position.qty),
                    "market_value": str(position.market_value),
                    "unrealized_pl": str(position.unrealized_pl),
                }
                for position in positions
            ],
        }

    def latest_stock_quote(self, symbol: str) -> PaperQuote:
        clean_symbol = symbol.strip().upper()
        quotes = self.stock_data.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=clean_symbol, feed=DataFeed.IEX)
        )
        quote = quotes[clean_symbol]
        return PaperQuote(
            symbol=clean_symbol,
            timestamp=(quote.timestamp.astimezone(timezone.utc).isoformat()
                       if quote.timestamp.tzinfo is not None and quote.timestamp.utcoffset() is not None
                       else quote.timestamp.isoformat()),
            bid=float(quote.bid_price),
            ask=float(quote.ask_price),
            bid_size=float(quote.bid_size),
            ask_size=float(quote.ask_size),
        )

    @staticmethod
    def _pilot_request(symbol: str, notional: Decimal, run_id: str | None = None) -> None:
        if symbol != "SPY":
            raise ValueError("the first supervised paper pilot supports SPY only")
        if (not isinstance(notional, Decimal) or not notional.is_finite()
                or not Decimal("1") <= notional <= Decimal("25")
                or notional != notional.quantize(Decimal("0.01"))):
            raise ValueError("pilot notional must be $1 to $25 in whole cents")
        if run_id is not None and (not isinstance(run_id, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id, re.ASCII) is None):
            raise ValueError("pilot run ID must be a bounded ASCII identifier")

    def _capture_preflight(self, symbol: str, *, account=None, capture_started_at=None) -> dict:
        """Collect bounded GET-only inputs; never persist account identity or credentials."""
        started = capture_started_at or datetime.now(timezone.utc)
        account = account if account is not None else self.trading.get_account()
        clock = self.trading.get_clock()
        asset = self.trading.get_asset(symbol)
        positions = self.trading.get_all_positions()
        orders = self.trading.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
        quote = self.latest_stock_quote(symbol)

        def value(obj, name):
            raw = getattr(obj, name, None)
            return getattr(raw, "value", raw)

        def instant(raw):
            if isinstance(raw, datetime):
                if raw.tzinfo is not None and raw.utcoffset() is not None:
                    return raw.astimezone(timezone.utc).isoformat()
                return raw.isoformat()
            return raw

        return {
            "capture_started_at": started.isoformat(),
            "account": {"id": str(account.id), **{name: value(account, name) for name in (
                "status", "currency", "trading_blocked", "account_blocked", "trade_suspended_by_user",
                "cash", "equity", "last_equity", "buying_power")}},
            "clock": {"is_open": value(clock, "is_open"), "timestamp": instant(value(clock, "timestamp")),
                      "next_close": instant(value(clock, "next_close"))},
            "asset": {name: value(asset, name) for name in (
                "symbol", "status", "asset_class", "tradable", "fractionable")},
            "positions": [{"symbol": value(p, "symbol"), "qty": value(p, "qty"),
                           "market_value": value(p, "market_value")} for p in positions],
            "open_orders": [{"present": True} for _ in orders],
            "quote": {"symbol": quote.symbol, "timestamp": quote.timestamp, "feed": "iex",
                      **{name: str(getattr(quote, name)) for name in ("bid", "ask", "bid_size", "ask_size")}},
        }

    def preflight_stock(self, symbol: str, notional: Decimal) -> dict:
        """Read-only conditions, not an authorization token or a reserved order slot."""
        symbol = self._symbol(symbol)
        self._pilot_request(symbol, notional)
        snapshot = self._capture_preflight(symbol)
        now = datetime.now(timezone.utc)
        result = evaluate_preflight(snapshot, symbol=symbol, notional=notional, now=now)
        state = PaperSessionGuard(PAPER_PILOT_DIRECTORY, snapshot["account"]["id"]).inspect(now=now)
        local_reasons = list(state["reasons"])
        if self._incident_path.exists() or any(p.exists() for p in self._legacy_incident_paths):
            local_reasons.append("LOCAL_RECONCILIATION_REQUIRED")
        return {"mode": "preflight", "status": "BLOCKED" if result["status"] != "PASS" or local_reasons else "PASS",
                "conditions": result, "local_state": state, "local_reasons": sorted(set(local_reasons)),
                "execution_authorized": False, "orders_submitted": 0,
                "limitations": ["Sequential GET snapshots are not atomic broker state.",
                                "Conditions are rechecked under the local lock before any authorized entry.",
                                "IEX quotes are not a consolidated NBBO or a fill-price guarantee."]}

    def submit_cancel_smoke(self, symbol: str, quantity: Decimal) -> dict[str, Any]:
        raise RuntimeError("legacy submit-cancel is disabled; use read-only preflight or the guarded pilot")

    def round_trip_stock(self, symbol: str, notional: Decimal, *, run_id: str) -> dict[str, Any]:
        if getattr(self, "_read_only", False):
            raise RuntimeError("read-only service cannot submit orders")
        self._require_gate("ENABLE_ALPACA_PAPER", "YES_I_UNDERSTAND")
        self._require_gate(
            "ENABLE_ALPACA_PAPER_ROUND_TRIP", "YES_RUN_SMALL_ROUND_TRIP"
        )
        clean_symbol = self._symbol(symbol)
        self._pilot_request(clean_symbol, notional, run_id)
        if run_id is None:
            raise ValueError("an explicit pilot run ID is required")
        with self._operation(clean_symbol, require_open=True, pilot=(run_id, notional)) as (ticket, account_id, snapshot):
            def before_entry():
                current = evaluate_preflight(snapshot, symbol=clean_symbol, notional=notional,
                                             now=datetime.now(timezone.utc))
                if current["status"] != "PASS":
                    raise PaperReconciliationRequired("paper preflight expired before entry submission")

            entry_request = ExactMarketOrderRequest(
                symbol=clean_symbol, notional=notional, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                client_order_id=ticket.entry_client_order_id,
            )
            entry = self._submit(entry_request, verify=True, before_send=before_entry)
            filled_entry = self._settle_order(entry.id, expected=entry_request)
            self._verify_pilot_order(filled_entry, entry_request, entry.id)
            filled_qty = Decimal(str(filled_entry.filled_qty or "0"))
            if not filled_qty.is_finite() or filled_qty <= 0:
                raise RuntimeError(f"entry {entry.id} ended without a fill")
            # Only a confirmed terminal entry fixes cumulative quantity. A
            # partially filled then canceled entry must still be unwound.
            exit_request = ExactMarketOrderRequest(
                symbol=clean_symbol, qty=filled_qty, side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                client_order_id=ticket.exit_client_order_id,
            )
            exit_order = self._submit(exit_request, verify=True)
            filled_exit = self._settle_order(exit_order.id, expected=exit_request)
            self._verify_pilot_order(filled_exit, exit_request, exit_order.id)
            exit_qty = Decimal(str(filled_exit.filled_qty or "0"))
            if exit_qty != filled_qty:
                raise PaperReconciliationRequired(
                    f"exit {exit_order.id} left residual qty {filled_qty - exit_qty}; no duplicate exit sent"
                )
            result = {
                "mode": "round-trip", "symbol": clean_symbol, "notional": str(notional),
                "filled_qty": str(filled_qty), "entry_order_id": str(filled_entry.id),
                "entry_price": str(filled_entry.filled_avg_price),
                "exit_order_id": str(filled_exit.id), "exit_price": str(filled_exit.filled_avg_price),
            }
            # Entry quality gates never prevent reducing an already confirmed fill.
            # Completion requires another same-account, whole-account-flat snapshot.
            account = self.trading.get_account()
            if str(account.id) != account_id:
                raise PaperReconciliationRequired("paper account identity changed during the pilot")
            positions = self.trading.get_all_positions()
            orders = self.trading.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
            if positions or orders:
                raise PaperReconciliationRequired("paper account is not fully flat after the pilot")
            ticket.complete({key: result[key] for key in (
                "entry_order_id", "exit_order_id", "filled_qty", "entry_price", "exit_price")})
            result["post_trade_status"] = {"positions": [], "open_orders": 0, "account_identity_verified": True}
            result["run_id"] = run_id
            result["engineering_test_only"] = True
            self.audit.record("alpaca_paper_round_trip", time.time_ns(), result)
        return result

    @staticmethod
    def _symbol(symbol: str) -> str:
        clean = symbol.strip().upper()
        if not clean or len(clean) > 10 or not all(c.isascii() and (c.isalpha() or c in ".-") for c in clean):
            raise ValueError("stock symbol is invalid")
        return clean

    def _assert_flat(self, symbol: str) -> None:
        positions = self.trading.get_all_positions()
        orders = self.trading.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.OPEN, symbols=[symbol], limit=500,
        ))
        if any(p.symbol == symbol and Decimal(str(p.qty)) != 0 for p in positions) or orders:
            raise PaperReconciliationRequired(f"{symbol} has an existing position or open order")

    @contextmanager
    def _operation(self, symbol: str, require_open: bool = False, pilot=None):
        with self._lock_path.open("a") as lock, ExitStack() as resources:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("another local paper operation is running") from error
            if self._incident_path.exists():
                raise PaperReconciliationRequired(
                    f"previous lifecycle needs reconciliation: {self._incident_path}"
                )
            for legacy_path in self._legacy_incident_paths:
                if legacy_path.exists():
                    # Preserve the old evidence and establish a key-scoped block
                    # so selecting a different audit path cannot evade it later.
                    with self._incident_path.open("x", encoding="utf-8") as file:
                        json.dump({"legacy_marker": str(legacy_path)}, file)
                        file.flush()
                        os.fsync(file.fileno())
                    raise PaperReconciliationRequired(
                        f"legacy lifecycle needs reconciliation: {legacy_path}"
                    )
            started = datetime.now(timezone.utc)
            account = self.trading.get_account()
            if account.trading_blocked or getattr(account, "account_blocked", False):
                raise RuntimeError("paper account is trading-blocked")
            self._assert_flat(symbol)
            if require_open:
                clock = self.trading.get_clock()
                if not clock.is_open:
                    raise RuntimeError(f"US equity market is closed; next open is {clock.next_open}")
            ticket = None
            if pilot is not None:
                run_id, notional = pilot
                snapshot = self._capture_preflight(symbol, account=account, capture_started_at=started)
                now = datetime.now(timezone.utc)
                checks = evaluate_preflight(snapshot, symbol=symbol, notional=notional, now=now)
                if checks["status"] != "PASS":
                    raise RuntimeError("paper pilot preflight blocked: " + ", ".join(checks["reasons"]))
                guard = PaperSessionGuard(PAPER_PILOT_DIRECTORY, str(account.id))
                ticket = resources.enter_context(guard.operation(
                    run_id=run_id, symbol=symbol, notional=notional, now=now))
                # Reserved evidence remains in the audit, without the account ID.
                self.audit.record("alpaca_paper_preflight_passed", time.time_ns(), checks)
            marker = {"symbol": symbol, "started_ns": time.time_ns(), "audit": str(self.audit.path)}
            with self._incident_path.open("x", encoding="utf-8") as file:
                json.dump(marker, file)
                file.flush()
                os.fsync(file.fileno())
            try:
                self.audit.record("alpaca_paper_lifecycle_started", time.time_ns(), marker)
                yield (ticket, str(account.id), snapshot) if pilot is not None else None
                self._assert_flat(symbol)
                self.audit.record("alpaca_paper_reconciled_flat", time.time_ns(), {"symbol": symbol})
                self._incident_path.unlink()
            except Exception as error:
                self.audit.record("alpaca_paper_reconciliation_required", time.time_ns(), {
                    "symbol": symbol, "error_type": type(error).__name__,
                })
                raise

    def _verify_pilot_order(self, order: Any, request: Any, expected_id=None) -> None:
        """Never infer exposure from an unbound or malformed broker order object."""
        try:
            UUID(str(order.id))
            if (expected_id is not None and str(order.id) != str(expected_id)
                    or str(order.client_order_id) != request.client_order_id
                    or order.symbol != request.symbol
                    or getattr(order.side, "value", order.side) != request.side.value
                    or getattr(order.type, "value", order.type) != "market"):
                raise ValueError("mismatched order")
            quantity = Decimal(str(order.filled_qty))
            if not quantity.is_finite() or quantity < 0:
                raise ValueError("invalid fill quantity")
            if request.qty is not None and quantity > request.qty:
                raise ValueError("overfilled exit")
            if quantity > 0:
                price = Decimal(str(order.filled_avg_price))
                if not price.is_finite() or price <= 0:
                    raise ValueError("invalid fill price")
            seen = getattr(self, "_pilot_seen_fills", {})
            previous = seen.get(str(order.id))
            if previous is not None and (previous[0] != request.client_order_id or quantity < previous[1]):
                raise ValueError("cumulative quantity or order binding regressed")
            seen[str(order.id)] = (request.client_order_id, quantity)
            self._pilot_seen_fills = seen
        except Exception:
            raise PaperReconciliationRequired("broker order identity or fill evidence is inconsistent") from None

    def _submit(self, request: Any, *, verify: bool = False, before_send=None) -> Any:
        payload = request.to_request_fields()
        self.audit.record("alpaca_paper_order_intent", time.time_ns(), payload)
        if before_send is not None:
            before_send()
        try:
            order = self.trading.submit_order(order_data=request)
        except Exception as error:
            self.audit.record("alpaca_paper_submit_uncertain", time.time_ns(), {
                "client_order_id": request.client_order_id, "error_type": type(error).__name__,
            })
            # A transport timeout can follow successful acceptance. Recover the
            # same id; never submit a fresh order after an ambiguous response.
            try:
                order = self.trading.get_order_by_client_id(request.client_order_id)
            except Exception as lookup_error:
                raise PaperReconciliationRequired(
                    f"submission outcome unknown; reconcile client_order_id={request.client_order_id}"
                ) from lookup_error
        if verify:
            self._verify_pilot_order(order, request)
        self._record_order(order)
        return order

    def _record_order(self, order: Any) -> None:
        self.audit.record("alpaca_paper_order_state", time.time_ns(), {
            "order_id": str(order.id), "client_order_id": str(getattr(order, "client_order_id", "")),
            "status": _order_status(order), "filled_qty": str(order.filled_qty or "0"),
            "filled_avg_price": str(getattr(order, "filled_avg_price", None)),
        })

    def _settle_order(self, order_id: Any, *, expected=None) -> Any:
        try:
            return self._wait_for_terminal(order_id, timeout_seconds=30, expected=expected)
        except (TimeoutError, ConnectionError, OSError, APIError, RequestException):
            return self._cancel_and_reconcile(order_id, expected=expected)

    def _cancel_and_reconcile(self, order_id: Any, *, expected=None) -> Any:
        self.audit.record("alpaca_paper_cancel_intent", time.time_ns(), {"order_id": str(order_id)})
        try:
            self.trading.cancel_order_by_id(order_id)
        except Exception as error:
            # The order may have filled concurrently. Only a later terminal
            # observation can establish what actually happened.
            self.audit.record("alpaca_paper_cancel_uncertain", time.time_ns(), {
                "order_id": str(order_id), "error_type": type(error).__name__,
            })
        try:
            return self._wait_for_terminal(order_id, timeout_seconds=15, expected=expected)
        except Exception as error:
            raise PaperReconciliationRequired(
                f"order {order_id} terminal state is unconfirmed; cancellation was only requested"
            ) from error

    @staticmethod
    def _require_gate(name: str, expected: str = "YES_I_UNDERSTAND") -> None:
        if os.environ.get(name) != expected:
            raise RuntimeError(f"{name} safety gate is disabled")

    def _wait_for_fill(self, order_id: Any, timeout_seconds: int) -> Any:
        order = self._wait_for_terminal(order_id, timeout_seconds)
        if _order_status(order) != "filled":
            raise RuntimeError(f"order reached terminal status {order.status}")
        return order

    def _wait_for_terminal(self, order_id: Any, timeout_seconds: int, *, expected=None) -> Any:
        deadline = time.monotonic() + timeout_seconds
        last_state = None
        previous_quantity = Decimal("0")
        while True:
            last = self.trading.get_order_by_id(order_id)
            if expected is not None:
                self._verify_pilot_order(last, expected, order_id)
                quantity = Decimal(str(last.filled_qty))
                if quantity < previous_quantity:
                    raise PaperReconciliationRequired("broker cumulative fill quantity decreased")
                previous_quantity = quantity
            status = _order_status(last)
            state = (status, str(last.filled_qty))
            if state != last_state:
                self._record_order(last)
                last_state = state
            if status in TERMINAL_STATUSES:
                return last
            if time.monotonic() >= deadline:
                raise TimeoutError(f"order {order_id} did not reach terminal state; last status={status}")
            time.sleep(0.25)
