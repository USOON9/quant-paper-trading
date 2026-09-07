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
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

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
    def __init__(self, credentials: PaperCredentials, audit_path: Path) -> None:
        self.audit = AuditTrail(audit_path)
        self.trading = TradingClient(credentials.key_id, credentials.secret_key, paper=True)
        self.stock_data = StockHistoricalDataClient(credentials.key_id, credentials.secret_key)
        # One local lifecycle per paper API key, even with different audit paths.
        key_hash = hashlib.sha256(credentials.key_id.encode()).hexdigest()[:24]
        lock_directory = Path(tempfile.gettempdir()) / "quantpaper-order-locks"
        lock_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock_path = lock_directory / f"{key_hash}.lock"
        # Durable marker survives process death/reboot, unlike the ephemeral lock.
        # Its identity is the API key, not the user-selectable audit filename.
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
            timestamp=quote.timestamp.isoformat(),
            bid=float(quote.bid_price),
            ask=float(quote.ask_price),
            bid_size=float(quote.bid_size),
            ask_size=float(quote.ask_size),
        )

    def submit_cancel_smoke(self, symbol: str, quantity: Decimal) -> dict[str, Any]:
        self._require_gate("ENABLE_ALPACA_PAPER", "YES_I_UNDERSTAND")
        if not quantity.is_finite() or not 0 < quantity <= 10 or quantity != quantity.to_integral_value():
            raise ValueError("smoke-test quantity must be an integer from 1 to 10 shares")
        clean_symbol = self._symbol(symbol)
        with self._operation(clean_symbol):
            quote = self.latest_stock_quote(clean_symbol)
            if not Decimal(str(quote.bid)).is_finite() or quote.bid <= 0.01:
                raise RuntimeError("latest bid must exceed the smoke-test limit")
            limit_price = Decimal("0.01")
            client_id = f"qp-smoke-{uuid.uuid4().hex[:20]}"
            order = self._submit(ExactLimitOrderRequest(
                symbol=clean_symbol, qty=quantity, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY, limit_price=limit_price,
                client_order_id=client_id,
            ))
            final = self._cancel_and_reconcile(order.id)
            if Decimal(str(final.filled_qty or "0")) != 0:
                raise PaperReconciliationRequired(
                    f"smoke order {final.id} filled {final.filled_qty}; a low limit is not a no-fill guarantee"
                )
            result = {
                "mode": "submit-cancel", "quote": asdict(quote),
                "order_id": str(final.id), "client_order_id": client_id,
                "limit_price": str(limit_price), "final_status": _order_status(final),
                "filled_qty": str(final.filled_qty),
            }
        return result

    def round_trip_stock(self, symbol: str, notional: Decimal) -> dict[str, Any]:
        self._require_gate("ENABLE_ALPACA_PAPER", "YES_I_UNDERSTAND")
        self._require_gate(
            "ENABLE_ALPACA_PAPER_ROUND_TRIP", "YES_RUN_SMALL_ROUND_TRIP"
        )
        if not notional.is_finite() or not Decimal("1") <= notional <= Decimal("25"):
            raise ValueError("round-trip notional must be between $1 and $25")
        clean_symbol = self._symbol(symbol)
        with self._operation(clean_symbol, require_open=True):
            entry = self._submit(ExactMarketOrderRequest(
                symbol=clean_symbol, notional=notional, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                client_order_id=f"qp-entry-{uuid.uuid4().hex[:20]}",
            ))
            filled_entry = self._settle_order(entry.id)
            filled_qty = Decimal(str(filled_entry.filled_qty or "0"))
            if not filled_qty.is_finite() or filled_qty <= 0:
                raise RuntimeError(f"entry {entry.id} ended without a fill")
            # Only a confirmed terminal entry fixes cumulative quantity. A
            # partially filled then canceled entry must still be unwound.
            exit_order = self._submit(ExactMarketOrderRequest(
                symbol=clean_symbol, qty=filled_qty, side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                client_order_id=f"qp-exit-{uuid.uuid4().hex[:20]}",
            ))
            filled_exit = self._settle_order(exit_order.id)
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
            self.audit.record("alpaca_paper_round_trip", time.time_ns(), result)
        result["post_trade_status"] = self.status()
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
    def _operation(self, symbol: str, require_open: bool = False):
        with self._lock_path.open("a") as lock:
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
            account = self.trading.get_account()
            if account.trading_blocked or getattr(account, "account_blocked", False):
                raise RuntimeError("paper account is trading-blocked")
            self._assert_flat(symbol)
            if require_open:
                clock = self.trading.get_clock()
                if not clock.is_open:
                    raise RuntimeError(f"US equity market is closed; next open is {clock.next_open}")
            marker = {"symbol": symbol, "started_ns": time.time_ns(), "audit": str(self.audit.path)}
            with self._incident_path.open("x", encoding="utf-8") as file:
                json.dump(marker, file)
                file.flush()
                os.fsync(file.fileno())
            try:
                self.audit.record("alpaca_paper_lifecycle_started", time.time_ns(), marker)
                yield
                self._assert_flat(symbol)
                self.audit.record("alpaca_paper_reconciled_flat", time.time_ns(), {"symbol": symbol})
                self._incident_path.unlink()
            except Exception as error:
                self.audit.record("alpaca_paper_reconciliation_required", time.time_ns(), {
                    "symbol": symbol, "error_type": type(error).__name__,
                })
                raise

    def _submit(self, request: Any) -> Any:
        payload = request.to_request_fields()
        self.audit.record("alpaca_paper_order_intent", time.time_ns(), payload)
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
        self._record_order(order)
        return order

    def _record_order(self, order: Any) -> None:
        self.audit.record("alpaca_paper_order_state", time.time_ns(), {
            "order_id": str(order.id), "client_order_id": str(getattr(order, "client_order_id", "")),
            "status": _order_status(order), "filled_qty": str(order.filled_qty or "0"),
            "filled_avg_price": str(getattr(order, "filled_avg_price", None)),
        })

    def _settle_order(self, order_id: Any) -> Any:
        try:
            return self._wait_for_terminal(order_id, timeout_seconds=30)
        except (TimeoutError, ConnectionError, OSError, APIError, RequestException):
            return self._cancel_and_reconcile(order_id)

    def _cancel_and_reconcile(self, order_id: Any) -> Any:
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
            return self._wait_for_terminal(order_id, timeout_seconds=15)
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

    def _wait_for_terminal(self, order_id: Any, timeout_seconds: int) -> Any:
        deadline = time.monotonic() + timeout_seconds
        last_state = None
        while True:
            last = self.trading.get_order_by_id(order_id)
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
