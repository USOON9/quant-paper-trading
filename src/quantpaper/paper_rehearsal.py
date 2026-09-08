"""Offline, synthetic exercise of the production Paper round-trip lifecycle.

This does not test Alpaca acceptance, fill realism, profitability, or a model.
No service constructor, credentials, environment gates, or network are used.
All state is temporary, including account guards, key markers and audit chains.
The only bypass is an instance-local gate function on an explicitly synthetic
service; production class gates and the process environment remain untouched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid5

from . import alpaca_paper
from .audit import AuditTrail
from .paper_guard import PaperGuardError, PaperSessionGuard


SCENARIO_NAMES = (
    "whole_fill", "partial_entry_canceled", "duplicate_and_same_account",
    "stale_quote", "ambiguous_submission", "residual_exit",
)
_PARTIAL = Decimal("0.006487141")
_WHOLE = Decimal("0.01")
_PRICE = Decimal("500")


def _check(condition: bool) -> None:
    # Unlike Python's assert statement this also runs under python -O.
    if not condition:
        raise RuntimeError("Synthetic Paper rehearsal assertion failed")


def _quantity(value: Decimal) -> str:
    return "0" if value == 0 else format(value, "f")


class _SyntheticBroker:
    """An in-memory state machine, not an Alpaca client or broker simulator."""

    def __init__(self, mode: str, account_id: UUID) -> None:
        self.mode = mode
        self.account_id = account_id
        self.requests: list = []
        self.orders: dict = {}
        self.position = Decimal("0")
        self.confirmed_entry = Decimal("0")
        self.client_lookups = 0
        self.polls = 0

    def get_account(self):
        return SimpleNamespace(
            id=self.account_id, status="ACTIVE", currency="USD",
            trading_blocked=False, account_blocked=False, trade_suspended_by_user=False,
            cash="100000", equity="100000", last_equity="100000", buying_power="100000",
        )

    def get_clock(self):
        now = datetime.now(timezone.utc)
        return SimpleNamespace(is_open=True, timestamp=now,
                               next_close=now + timedelta(hours=1),
                               next_open=now + timedelta(days=1))

    def get_asset(self, symbol):
        _check(symbol == "SPY")
        return SimpleNamespace(symbol="SPY", status="active", asset_class="us_equity",
                               tradable=True, fractionable=True)

    def get_all_positions(self):
        if self.position == 0:
            return []
        return [SimpleNamespace(symbol="SPY", qty=_quantity(self.position),
                                market_value=_quantity(self.position * _PRICE))]

    def get_orders(self, *, filter):
        # Every observable synthetic order becomes terminal on its first poll.
        # Unknown submission is deliberately not claimed to be a known order.
        return []

    def submit_order(self, *, order_data):
        request = order_data
        _check(request.symbol == "SPY")
        _check(request.type.value == "market")
        _check(request.time_in_force.value == "day")
        _check(request.client_order_id not in {r.client_order_id for r in self.requests})
        self.requests.append(request)
        side = request.side.value
        _check(side in {"buy", "sell"})
        if side == "buy":
            _check(request.notional == Decimal("5") and request.qty is None)
            if self.mode == "ambiguous_submission":
                raise TimeoutError("Synthetic submission outcome is unknown")
            quantity = _PARTIAL if self.mode == "partial_entry_canceled" else _WHOLE
            self.confirmed_entry = quantity
            self.position += quantity
            status = "partially_filled" if self.mode == "partial_entry_canceled" else "filled"
        else:
            _check(request.notional is None and request.qty == self.confirmed_entry)
            quantity = Decimal("0.004") if self.mode == "residual_exit" else request.qty
            self.position -= quantity
            status = "canceled" if self.mode == "residual_exit" else "filled"
        order_id = uuid5(self.account_id, f"synthetic-order-{len(self.requests)}")
        order = SimpleNamespace(
            id=order_id, client_order_id=request.client_order_id, symbol=request.symbol,
            side=side, type="market", status=status, filled_qty=_quantity(quantity),
            filled_avg_price=_quantity(_PRICE),
        )
        self.orders[str(order_id)] = order
        return order

    def get_order_by_id(self, order_id):
        self.polls += 1
        order = self.orders[str(order_id)]
        if order.status == "partially_filled":
            # Cancellation is already broker-confirmed in this fixture. This
            # scenario does not exercise a real cancellation request or race.
            order = SimpleNamespace(**{**vars(order), "status": "canceled"})
            self.orders[str(order_id)] = order
        _check(order.status in {"filled", "canceled"})
        return order

    def get_order_by_client_id(self, client_order_id):
        self.client_lookups += 1
        _check(self.requests and self.requests[-1].client_order_id == client_order_id)
        if self.mode == "ambiguous_submission":
            raise TimeoutError("Synthetic lookup cannot establish acceptance")
        for order in self.orders.values():
            if order.client_order_id == client_order_id:
                return order
        raise RuntimeError("Synthetic order was not found")

    def cancel_order_by_id(self, order_id):
        # Unexpected polling/cancellation must fail immediately, never sleep.
        raise RuntimeError("Synthetic scenario unexpectedly required cancellation")


class _SyntheticQuotes:
    def __init__(self, *, stale: bool) -> None:
        self.stale = stale

    def get_stock_latest_quote(self, request):
        _check(request.symbol_or_symbols == "SPY")
        _check(request.feed.value == "iex")
        now = datetime.now(timezone.utc)
        return {"SPY": SimpleNamespace(
            timestamp=now - timedelta(seconds=3 if self.stale else 0),
            bid_price=499.99, ask_price=500.01, bid_size=100, ask_size=100,
        )}


def _service(root: Path, broker: _SyntheticBroker, *, key_alias: str):
    root.mkdir(parents=True, exist_ok=True)
    service = alpaca_paper.AlpacaPaperService.__new__(alpaca_paper.AlpacaPaperService)
    service._read_only = False
    service.audit = AuditTrail(root / f"{key_alias}.audit.jsonl")
    service.trading = broker
    service.stock_data = _SyntheticQuotes(stale=broker.mode == "stale_quote")
    service._lock_path = root / f"{key_alias}.lock"
    service._incident_path = root / f"{key_alias}.reconciliation.json"
    service._legacy_incident_paths = ()
    # This never changes AlpacaPaperService._require_gate or os.environ.
    service._require_gate = lambda *args, **kwargs: None
    return service


def _run(service, run_id: str) -> tuple[str, dict | None]:
    try:
        result = service.round_trip_stock("SPY", Decimal("5"), run_id=run_id)
    except PaperGuardError as error:
        _check(error.code in {"RUN_ID_REUSED", "DAILY_LIMIT_REACHED", "RECONCILIATION_REQUIRED"})
        return error.code, None
    except alpaca_paper.PaperReconciliationRequired:
        return "RECONCILIATION_REQUIRED", None
    except RuntimeError as error:
        if str(error).startswith("paper pilot preflight blocked: ") and "QUOTE_STALE" in str(error):
            return "QUOTE_STALE_BLOCKED", None
        raise RuntimeError("Synthetic Paper rehearsal failed unexpectedly") from None
    _check(result["engineering_test_only"] is True)
    _check(result["post_trade_status"] == {
        "positions": [], "open_orders": 0, "account_identity_verified": True,
    })
    return "COMPLETED_FLAT", result


def _audit_events(service) -> list[str]:
    with service.audit.path.open(encoding="utf-8") as handle:
        AuditTrail._verify(handle)
        handle.seek(0)
        return [json.loads(line)["event"] for line in handle]


def _scenario(root: Path, name: str, index: int) -> dict:
    broker = _SyntheticBroker(name, UUID(int=100 + index))
    service = _service(root, broker, key_alias="synthetic-key-one")
    state, result = _run(service, "synthetic-first")
    initial_submit_count = len(broker.requests)
    restart_states = []
    if name == "duplicate_and_same_account":
        restart_states.append(_run(service, "synthetic-first")[0])
        alias = _service(root, broker, key_alias="synthetic-key-two")
        restart_states.append(_run(alias, "synthetic-second")[0])
    elif name in {"ambiguous_submission", "residual_exit"}:
        restart_states.append(_run(service, "synthetic-restart")[0])
        alias = _service(root, broker, key_alias="synthetic-key-two")
        restart_states.append(_run(alias, "synthetic-restart-other-key")[0])
    guard = PaperSessionGuard(alpaca_paper.PAPER_PILOT_DIRECTORY, str(broker.account_id))
    guard_state = guard.inspect(now=datetime.now(timezone.utc))
    events = _audit_events(service)
    buys = [request for request in broker.requests if request.side.value == "buy"]
    sells = [request for request in broker.requests if request.side.value == "sell"]
    observed = {
        "state": state,
        "synthetic_submit_calls": len(broker.requests),
        "synthetic_entry_submits": len(buys),
        "synthetic_exit_submits": len(sells),
        "synthetic_client_id_lookups": broker.client_lookups,
        "confirmed_entry_quantity": None if name == "ambiguous_submission" else _quantity(broker.confirmed_entry),
        "exit_requested_quantity": _quantity(sells[0].qty) if sells else None,
        "remaining_known_quantity": None if name == "ambiguous_submission" else _quantity(broker.position),
        "key_marker_retained": service._incident_path.exists(),
        "account_guard_blocked": guard_state["blocked"],
        "account_guard_reasons": guard_state["reasons"],
        "restart_states": restart_states,
        "additional_restart_submits": len(broker.requests) - initial_submit_count,
        "audit_chain_verified": True,
        "order_intents_recorded": events.count("alpaca_paper_order_intent"),
    }
    partial = name == "partial_entry_canceled"
    stale = name == "stale_quote"
    ambiguous = name == "ambiguous_submission"
    residual = name == "residual_exit"
    unresolved = ambiguous or residual
    entry_quantity = _quantity(_PARTIAL if partial else _WHOLE)
    expected = {
        "state": "QUOTE_STALE_BLOCKED" if stale else "RECONCILIATION_REQUIRED" if unresolved else "COMPLETED_FLAT",
        "synthetic_submit_calls": 0 if stale else 1 if ambiguous else 2,
        "synthetic_entry_submits": 0 if stale else 1,
        "synthetic_exit_submits": 0 if stale or ambiguous else 1,
        "synthetic_client_id_lookups": 1 if ambiguous else 0,
        "confirmed_entry_quantity": None if ambiguous else "0" if stale else entry_quantity,
        "exit_requested_quantity": None if stale or ambiguous else entry_quantity,
        "remaining_known_quantity": None if ambiguous else "0.006" if residual else "0",
        "key_marker_retained": unresolved,
        "account_guard_blocked": not stale,
        "account_guard_reasons": [] if stale else ["RECONCILIATION_REQUIRED", "DAILY_LIMIT_REACHED"] if unresolved else ["DAILY_LIMIT_REACHED"],
        "restart_states": ["RUN_ID_REUSED", "DAILY_LIMIT_REACHED"] if name == "duplicate_and_same_account" else ["RECONCILIATION_REQUIRED", "RECONCILIATION_REQUIRED"] if unresolved else [],
        "additional_restart_submits": 0,
        "audit_chain_verified": True,
        "order_intents_recorded": 0 if stale else 1 if ambiguous else 2,
    }
    _check(observed == expected)
    _check((result is not None) == (not stale and not unresolved))
    _check(broker.polls == (0 if stale or ambiguous else 2))
    if result is not None:
        _check(result["filled_qty"] == entry_quantity)
        _check("alpaca_paper_reconciled_flat" in events)
    return {"scenario": name, "passed": True, "expected": expected, "observed": observed}


def rehearse() -> dict:
    """Run all six local scenarios; unexpected behavior raises, never passes.

    The CLI uses this in a fresh process. The temporary module-path patch is
    scoped by a context manager and restored even on failure. No real account,
    credentials, existing state directory, or environment value is inspected.
    """
    with TemporaryDirectory(prefix="quantpaper-synthetic-rehearsal-") as directory:
        root = Path(directory).resolve()
        with patch.object(alpaca_paper, "PAPER_PILOT_DIRECTORY", root / "account-guards"):
            scenarios = [_scenario(root / name, name, index)
                         for index, name in enumerate(SCENARIO_NAMES)]
    return {
        "mode": "offline-synthetic-paper-rehearsal", "status": "PASS",
        "scenarios_passed": len(scenarios), "scenarios_total": len(SCENARIO_NAMES),
        "production_lifecycle": "AlpacaPaperService.round_trip_stock",
        "synthetic_submit_calls": sum(row["observed"]["synthetic_submit_calls"] for row in scenarios),
        "broker_requests": 0, "orders_submitted_to_alpaca": 0,
        "environment_unchanged": True, "model_used": False,
        "paper_trading_enabled": False, "execution_authorized": False,
        "temporary_state_removed": True, "scenarios": scenarios,
        "limitations": [
            "All account, clock, quote, order and fill responses are synthetic in-memory fixtures.",
            "This tests local lifecycle and safety behavior, not Alpaca acceptance, connectivity or execution quality.",
            "Synthetic fills and prices are not market observations, trading results or model predictions.",
            "The partial-entry fixture supplies confirmed cancellation; it does not test a cancellation race.",
            "No production gate or environment value is read or changed; only the synthetic instance bypasses gates.",
            "Unknown submission and residual exposure require reconciliation; the rehearsal never retries a POST.",
        ],
    }
