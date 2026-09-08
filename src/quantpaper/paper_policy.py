"""Pure, fail-closed preflight for a narrow SPY Paper engineering pilot.

A PASS describes this normalized snapshot only. It never authorizes execution,
approves an investment strategy, changes an order gate, or calls a broker.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import re


_TOP = frozenset({"account", "clock", "asset", "positions", "open_orders", "quote", "capture_started_at"})
_ACCOUNT = frozenset({"id", "status", "currency", "trading_blocked", "account_blocked",
                      "trade_suspended_by_user", "cash", "equity", "last_equity", "buying_power"})
_CLOCK = frozenset({"is_open", "timestamp", "next_close"})
_ASSET = frozenset({"symbol", "status", "asset_class", "tradable", "fractionable"})
_QUOTE = frozenset({"symbol", "timestamp", "bid", "ask", "bid_size", "ask_size", "feed"})
_ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)\Z", re.ASCII)
_DECIMAL = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?\Z", re.ASCII)
_CODES = (
    "ACCOUNT_BLOCKED", "ACCOUNT_CURRENCY_NOT_USD", "ACCOUNT_DAILY_LOSS_LIMIT", "ACCOUNT_ID_INVALID",
    "ACCOUNT_NOT_ACTIVE", "ACCOUNT_NUMERIC_INVALID", "ACCOUNT_SCHEMA_INVALID", "ACCOUNT_NOT_FLAT",
    "ASSET_NOT_ACTIVE", "ASSET_NOT_FRACTIONABLE", "ASSET_NOT_TRADABLE", "ASSET_NOT_US_EQUITY",
    "ASSET_SCHEMA_INVALID", "ASSET_SYMBOL_MISMATCH", "BUYING_POWER_INSUFFICIENT", "CASH_RESERVE_INSUFFICIENT",
    "CLOCK_SCHEMA_INVALID", "CLOCK_SKEW_EXCEEDED", "CLOCK_TIMESTAMP_INVALID", "CLOSE_BUFFER_INSUFFICIENT",
    "COLLECTION_START_FUTURE", "COLLECTION_START_INVALID", "COLLECTION_TOO_SLOW", "EQUITY_NOT_POSITIVE",
    "LAST_EQUITY_NOT_POSITIVE", "MARKET_NOT_OPEN", "NEXT_CLOSE_INVALID", "NOTIONAL_INVALID",
    "NOW_INVALID", "OPEN_ORDERS_PRESENT", "OPEN_ORDERS_INVALID", "POSITIONS_INVALID", "QUOTE_CROSSED",
    "QUOTE_FEED_MISMATCH", "QUOTE_FUTURE", "QUOTE_NUMERIC_INVALID", "QUOTE_SCHEMA_INVALID", "QUOTE_STALE",
    "QUOTE_SYMBOL_MISMATCH", "QUOTE_TIMESTAMP_INVALID", "SNAPSHOT_NOT_JSON", "SNAPSHOT_SCHEMA_INVALID",
    "SPREAD_TOO_WIDE", "SYMBOL_NOT_ALLOWED",
)


def policy_contract() -> dict:
    """Return a fresh, complete hashable description of the engineering gates."""
    return {
        "contract_id": "spy_paper_engineering_preflight_v1",
        "allowed_symbols": ["SPY"], "notional_usd_min": "1.00", "notional_usd_max": "25.00",
        "notional_increment_usd": "0.01", "cash_reserve_usd": "1.00",
        "required_account_status": "ACTIVE", "required_account_currency": "USD",
        "all_account_block_flags_must_be_false": ["trading_blocked", "account_blocked", "trade_suspended_by_user"],
        "equity_and_last_equity": "strictly positive",
        "cash_rule": "cash >= notional + 1.00", "buying_power_rule": "buying_power >= notional",
        "daily_loss_proxy": "block when last_equity - equity >= 5.00 USD",
        "account_flat_rule": "positions must be an empty list; zero-quantity entries are not silently ignored",
        "open_orders_rule": "open_orders must be an empty list",
        "required_asset": {"status": "active", "asset_class": "us_equity", "tradable": True, "fractionable": True},
        "market_open_required": True, "maximum_broker_clock_skew_seconds": 5,
        "maximum_collection_seconds": 10, "minimum_seconds_before_close_exclusive": 300,
        "quote_feed": "iex", "maximum_quote_age_seconds": 2,
        "quote_prices_and_sizes": "finite positive decimal strings; ask >= bid; a locked quote is allowed",
        "spread_formula": "(ask - bid) / ((ask + bid) / 2) * 10000",
        "maximum_spread_bps": "20", "threshold_policy": "maximum age/skew/spread inclusive; close buffer strictly greater; daily-loss block inclusive",
        "timestamp_policy": "UTC-aware ISO strings with at most microsecond precision; quote and capture start cannot be later than evaluated_at",
        "numeric_policy": "no booleans, nonfinite values, exponent strings, coercion, or imputation; notional is Decimal with exact cent granularity; arithmetic uses an isolated precision-100 ROUND_HALF_EVEN context",
        "input_schema": {"top": sorted(_TOP), "account": sorted(_ACCOUNT), "clock": sorted(_CLOCK),
                         "asset": sorted(_ASSET), "quote": sorted(_QUOTE),
                         "positions_required_fields": ["symbol", "qty", "market_value"],
                         "positions_and_orders_max_items": 1000, "maximum_snapshot_json_bytes": 1_000_000},
        "reasons": list(_CODES), "reason_order": "lexicographically sorted unique fixed codes",
        "snapshot_hash": "SHA-256 of canonical sorted-key exact normalized input JSON; null for invalid or oversized JSON",
        "privacy": "account IDs, balances, positions, orders and raw quote contents are not returned",
        "research_engineering_only": True, "execution_authorized": False,
        "limitations": [
            "SPY-only is a first engineering-pilot boundary, not an investment recommendation.",
            "The one-dollar cash reserve is a fixed engineering buffer, not a commission or fee estimate.",
            "The account equity-change proxy includes cash-flow effects and is not an attributed trading-loss ledger.",
            "IEX is one feed, not a verified consolidated NBBO; quotes do not guarantee fills or displayed liquidity.",
            "Time, spread and risk limits are fixed research assumptions, not calibrated profitability or execution claims.",
            "A snapshot can change after evaluation; PASS is not an order, fill, broker authorization, or model admission.",
            "Caller must independently enforce the Paper endpoint, account selection, idempotency and execution authorization.",
        ],
    }


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def _json_types(value, depth=0):
    if depth > 64:
        raise ValueError("invalid JSON depth")
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("invalid JSON key")
        for item in value.values():
            _json_types(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_types(item, depth + 1)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise ValueError("invalid JSON value")


def _snapshot_hash(snapshot):
    try:
        _json_types(snapshot)
        encoded = _canonical(snapshot)
        if len(encoded) > 1_000_000:
            return None
        return hashlib.sha256(encoded).hexdigest()
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        return None


def _timestamp(value):
    if not isinstance(value, str) or not _ISO.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.utcoffset() == timedelta(0) else None
    except (ValueError, OverflowError):
        return None


def _now(value):
    try:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
            return None
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds") if value is not None else None


def _number(value):
    if not isinstance(value, str) or len(value) > 64 or not _DECIMAL.fullmatch(value):
        return None
    try:
        parsed = Decimal(value)
        return parsed if parsed.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _section(snapshot, name, expected, reasons):
    value = snapshot.get(name)
    if not isinstance(value, dict) or set(value) != expected:
        reasons.add(f"{name.upper()}_SCHEMA_INVALID")
        return value if isinstance(value, dict) else {}
    return value


def evaluate_preflight(snapshot: dict, *, symbol: str, notional: Decimal, now: datetime) -> dict:
    """Inspect only; collect all independently evaluable failures without I/O."""
    reasons = set()
    snapshot_hash = _snapshot_hash(snapshot)
    if snapshot_hash is None:
        reasons.add("SNAPSHOT_NOT_JSON")
    if not isinstance(snapshot, dict) or set(snapshot) != _TOP:
        reasons.add("SNAPSHOT_SCHEMA_INVALID")
    data = snapshot if isinstance(snapshot, dict) else {}
    safe_symbol = symbol if type(symbol) is str and symbol == "SPY" else None
    if safe_symbol is None:
        reasons.add("SYMBOL_NOT_ALLOWED")
    amount = None
    with localcontext(Context(prec=100, rounding=ROUND_HALF_EVEN)):
        try:
            if (type(notional) is Decimal and notional.is_finite() and Decimal("1") <= notional <= Decimal("25")
                    and notional == notional.quantize(Decimal("0.01"))):
                amount = notional
        except (InvalidOperation, ValueError):
            pass
    if amount is None:
        reasons.add("NOTIONAL_INVALID")
    evaluated = _now(now)
    if evaluated is None:
        reasons.add("NOW_INVALID")

    account = _section(data, "account", _ACCOUNT, reasons)
    if not isinstance(account.get("id"), str) or not 1 <= len(account["id"]) <= 128 or not account["id"].strip():
        reasons.add("ACCOUNT_ID_INVALID")
    if account.get("status") != "ACTIVE":
        reasons.add("ACCOUNT_NOT_ACTIVE")
    if account.get("currency") != "USD":
        reasons.add("ACCOUNT_CURRENCY_NOT_USD")
    if any(account.get(field) is not False for field in ("trading_blocked", "account_blocked", "trade_suspended_by_user")):
        reasons.add("ACCOUNT_BLOCKED")
    values = {field: _number(account.get(field)) for field in ("cash", "equity", "last_equity", "buying_power")}
    if any(value is None for value in values.values()):
        reasons.add("ACCOUNT_NUMERIC_INVALID")
    for field, code in (("equity", "EQUITY_NOT_POSITIVE"), ("last_equity", "LAST_EQUITY_NOT_POSITIVE")):
        if values[field] is not None and values[field] <= 0:
            reasons.add(code)
    with localcontext(Context(prec=100, rounding=ROUND_HALF_EVEN)):
        if amount is not None:
            if values["cash"] is not None and values["cash"] < amount + Decimal("1"):
                reasons.add("CASH_RESERVE_INSUFFICIENT")
            if values["buying_power"] is not None and values["buying_power"] < amount:
                reasons.add("BUYING_POWER_INSUFFICIENT")
        if values["equity"] is not None and values["last_equity"] is not None:
            if values["last_equity"] - values["equity"] >= Decimal("5"):
                reasons.add("ACCOUNT_DAILY_LOSS_LIMIT")

    positions = data.get("positions")
    if not isinstance(positions, list) or len(positions) > 1000:
        reasons.add("POSITIONS_INVALID")
    else:
        if positions:
            reasons.add("ACCOUNT_NOT_FLAT")
        for row in positions:
            if (not isinstance(row, dict) or not isinstance(row.get("symbol"), str)
                    or not row["symbol"].strip() or len(row["symbol"]) > 64
                    or _number(row.get("qty")) is None or _number(row.get("market_value")) is None):
                reasons.add("POSITIONS_INVALID")
    orders = data.get("open_orders")
    if not isinstance(orders, list) or len(orders) > 1000 or any(not isinstance(row, dict) for row in orders):
        reasons.add("OPEN_ORDERS_INVALID")
    elif orders:
        reasons.add("OPEN_ORDERS_PRESENT")

    asset = _section(data, "asset", _ASSET, reasons)
    if safe_symbol is None or asset.get("symbol") != safe_symbol:
        reasons.add("ASSET_SYMBOL_MISMATCH")
    if asset.get("status") != "active":
        reasons.add("ASSET_NOT_ACTIVE")
    if asset.get("asset_class") != "us_equity":
        reasons.add("ASSET_NOT_US_EQUITY")
    if asset.get("tradable") is not True:
        reasons.add("ASSET_NOT_TRADABLE")
    if asset.get("fractionable") is not True:
        reasons.add("ASSET_NOT_FRACTIONABLE")

    clock = _section(data, "clock", _CLOCK, reasons)
    if clock.get("is_open") is not True:
        reasons.add("MARKET_NOT_OPEN")
    broker_time = _timestamp(clock.get("timestamp"))
    if broker_time is None:
        reasons.add("CLOCK_TIMESTAMP_INVALID")
    elif evaluated is not None and abs((broker_time - evaluated).total_seconds()) > 5:
        reasons.add("CLOCK_SKEW_EXCEEDED")
    closing = _timestamp(clock.get("next_close"))
    if closing is None:
        reasons.add("NEXT_CLOSE_INVALID")
    elif evaluated is not None and (closing - evaluated).total_seconds() <= 300:
        reasons.add("CLOSE_BUFFER_INSUFFICIENT")
    capture = _timestamp(data.get("capture_started_at"))
    if capture is None:
        reasons.add("COLLECTION_START_INVALID")
    elif evaluated is not None:
        duration = (evaluated - capture).total_seconds()
        if duration < 0:
            reasons.add("COLLECTION_START_FUTURE")
        elif duration > 10:
            reasons.add("COLLECTION_TOO_SLOW")

    quote = _section(data, "quote", _QUOTE, reasons)
    if safe_symbol is None or quote.get("symbol") != safe_symbol:
        reasons.add("QUOTE_SYMBOL_MISMATCH")
    if quote.get("feed") != "iex":
        reasons.add("QUOTE_FEED_MISMATCH")
    quoted = _timestamp(quote.get("timestamp"))
    quote_age = None
    if quoted is None:
        reasons.add("QUOTE_TIMESTAMP_INVALID")
    elif evaluated is not None:
        quote_age = (evaluated - quoted).total_seconds()
        if quote_age < 0:
            reasons.add("QUOTE_FUTURE")
        elif quote_age > 2:
            reasons.add("QUOTE_STALE")
    prices = {field: _number(quote.get(field)) for field in ("bid", "ask", "bid_size", "ask_size")}
    spread = None
    if any(value is None or value <= 0 for value in prices.values()):
        reasons.add("QUOTE_NUMERIC_INVALID")
    elif prices["ask"] < prices["bid"]:
        reasons.add("QUOTE_CROSSED")
    else:
        with localcontext(Context(prec=100, rounding=ROUND_HALF_EVEN)):
            spread_decimal = (prices["ask"] - prices["bid"]) / ((prices["ask"] + prices["bid"]) / 2) * 10000
            spread = float(spread_decimal)
            if spread_decimal > Decimal("20"):
                reasons.add("SPREAD_TOO_WIDE")

    return {
        "status": "BLOCKED" if reasons else "PASS", "reasons": sorted(reasons),
        "symbol": safe_symbol, "notional_usd": format(amount, ".2f") if amount is not None else None,
        "timestamps": {"evaluated_at": _iso(evaluated), "capture_started_at": _iso(capture),
                       "broker_clock_at": _iso(broker_time), "next_close_at": _iso(closing), "quote_at": _iso(quoted)},
        "quote_age_seconds": quote_age, "spread_bps": spread, "snapshot_hash": snapshot_hash,
        "policy_hash": hashlib.sha256(_canonical(policy_contract())).hexdigest(),
        "research_engineering_only": True, "execution_authorized": False,
    }
