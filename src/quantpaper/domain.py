"""Core domain types and exact portfolio accounting.

Prices, quantities and money use Decimal. Timestamps use UTC epoch nanoseconds.
Keeping these rules in one module prevents subtle discrepancies between the
backtest, local paper venue and a future broker adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


ZERO = Decimal("0")


class AssetClass(StrEnum):
    EQUITY = "equity"
    CRYPTO = "crypto"
    OPTION = "option"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> Decimal:
        return Decimal("1") if self is Side.BUY else Decimal("-1")


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(StrEnum):
    NEW = "new"
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    asset_class: AssetClass
    tick_size: Decimal
    lot_size: Decimal
    multiplier: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if not self.symbol or any(not v.is_finite() or v <= ZERO for v in (self.tick_size, self.lot_size, self.multiplier)):
            raise ValueError("instrument requires a symbol and positive finite tick, lot and multiplier")


@dataclass(frozen=True, slots=True)
class Quote:
    ts_ns: int
    instrument: Instrument
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal

    def __post_init__(self) -> None:
        if any(not v.is_finite() for v in (self.bid, self.ask, self.bid_size, self.ask_size)):
            raise ValueError("quote values must be finite")
        if self.ts_ns < 0 or self.bid <= ZERO or self.ask < self.bid:
            raise ValueError("invalid quote")
        if self.bid_size <= ZERO or self.ask_size <= ZERO:
            raise ValueError("quote sizes must be positive")

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def imbalance(self) -> Decimal:
        return (self.bid_size - self.ask_size) / (self.bid_size + self.ask_size)


@dataclass(slots=True)
class Order:
    order_id: str
    instrument: Instrument
    side: Side
    quantity: Decimal
    order_type: OrderType
    created_ns: int
    limit_price: Decimal | None = None
    status: OrderStatus = OrderStatus.NEW
    reject_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.order_id or self.created_ns < 0:
            raise ValueError("order requires an id and nonnegative timestamp")
        if not self.quantity.is_finite() or self.quantity <= ZERO:
            raise ValueError("order quantity must be positive")
        if self.quantity % self.instrument.lot_size != ZERO:
            raise ValueError("quantity does not respect lot size")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit order requires limit_price")
        if self.limit_price is not None and (
            not self.limit_price.is_finite() or self.limit_price <= ZERO
            or self.limit_price % self.instrument.tick_size != ZERO
        ):
            raise ValueError("limit_price must be positive, finite and tick-aligned")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("market order cannot have limit_price")


@dataclass(frozen=True, slots=True)
class Fill:
    order_id: str
    ts_ns: int
    instrument: Instrument
    side: Side
    quantity: Decimal
    price: Decimal
    commission: Decimal

    def __post_init__(self) -> None:
        if not self.order_id or self.ts_ns < 0:
            raise ValueError("fill requires an id and nonnegative timestamp")
        if any(not v.is_finite() for v in (self.quantity, self.price, self.commission)):
            raise ValueError("fill values must be finite")
        if self.quantity <= ZERO or self.price <= ZERO or self.commission < ZERO:
            raise ValueError("fill quantity and price must be positive; commission nonnegative")


@dataclass(slots=True)
class Position:
    quantity: Decimal = ZERO
    average_price: Decimal = ZERO
    realized_pnl: Decimal = ZERO


@dataclass(slots=True)
class Portfolio:
    starting_cash: Decimal
    cash: Decimal = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)
    fees_paid: Decimal = ZERO

    def __post_init__(self) -> None:
        if not self.starting_cash.is_finite() or self.starting_cash <= ZERO:
            raise ValueError("starting_cash must be positive and finite")
        self.cash = self.starting_cash

    def position(self, symbol: str) -> Position:
        return self.positions.setdefault(symbol, Position())

    def apply_fill(self, fill: Fill) -> None:
        position = self.position(fill.instrument.symbol)
        old_qty = position.quantity
        signed_fill = fill.side.sign * fill.quantity
        new_qty = old_qty + signed_fill
        multiplier = fill.instrument.multiplier

        if old_qty == ZERO or old_qty * signed_fill > ZERO:
            old_cost = abs(old_qty) * position.average_price
            new_cost = fill.quantity * fill.price
            position.average_price = (old_cost + new_cost) / (abs(old_qty) + fill.quantity)
        else:
            closing_qty = min(abs(old_qty), fill.quantity)
            direction = Decimal("1") if old_qty > ZERO else Decimal("-1")
            position.realized_pnl += (
                (fill.price - position.average_price) * closing_qty * direction * multiplier
            )
            if new_qty == ZERO:
                position.average_price = ZERO
            elif old_qty * new_qty < ZERO:
                position.average_price = fill.price

        position.quantity = new_qty
        self.cash -= signed_fill * fill.price * multiplier + fill.commission
        self.fees_paid += fill.commission

    def equity(self, marks: dict[str, Decimal], instruments: dict[str, Instrument]) -> Decimal:
        value = self.cash
        for symbol, position in self.positions.items():
            if position.quantity != ZERO:
                if symbol not in marks or symbol not in instruments:
                    raise ValueError(f"missing mark/instrument for open position {symbol}")
                value += position.quantity * marks[symbol] * instruments[symbol].multiplier
        return value

    def gross_notional(
        self, marks: dict[str, Decimal], instruments: dict[str, Instrument]
    ) -> Decimal:
        self.equity(marks, instruments)  # Validate completeness consistently.
        return sum((
            abs(position.quantity) * marks[symbol] * instruments[symbol].multiplier
            for symbol, position in self.positions.items() if position.quantity != ZERO
        ), ZERO)
