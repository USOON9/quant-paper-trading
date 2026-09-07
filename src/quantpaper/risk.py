"""Independent pre-trade risk gate and kill switch."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .domain import Instrument, Order, OrderStatus, Portfolio, Quote, Side, ZERO


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_order_notional: Decimal
    max_symbol_notional: Decimal
    max_gross_notional: Decimal
    max_daily_loss: Decimal
    max_orders_per_second: int


class RiskEngine:
    def __init__(self, limits: RiskLimits, starting_equity: Decimal) -> None:
        self.limits = limits
        self.starting_equity = starting_equity
        self.kill_switch = False
        self._recent_orders: deque[int] = deque()

    def approve(
        self,
        order: Order,
        quote: Quote,
        portfolio: Portfolio,
        marks: dict[str, Decimal],
        instruments: dict[str, Instrument],
        pending_orders: Iterable[Order] = (),
    ) -> tuple[bool, str]:
        if self.kill_switch:
            return False, "kill switch active"
        if order.instrument != quote.instrument or order.created_ns != quote.ts_ns:
            return False, "order quote mismatch"
        equity = portfolio.equity(marks, instruments)
        if self.starting_equity - equity >= self.limits.max_daily_loss:
            self.kill_switch = True
            return False, "daily loss limit breached"

        price = order.limit_price or (quote.ask if order.side.value == "buy" else quote.bid)
        order_notional = order.quantity * price * order.instrument.multiplier
        if order_notional > self.limits.max_order_notional:
            return False, "max order notional exceeded"

        # Reserve each side independently: opposing pending orders may not
        # both fill, so netting their signed quantities understates exposure.
        pending = [p for p in pending_orders if p.status in (OrderStatus.NEW, OrderStatus.PENDING)] + [order]
        quantities = {s: p.quantity for s, p in portfolio.positions.items()}
        symbols = set(quantities) | {p.instrument.symbol for p in pending}
        projected = {}
        for symbol in symbols:
            buys = sum((p.quantity for p in pending if p.instrument.symbol == symbol and p.side is Side.BUY), ZERO)
            sells = sum((p.quantity for p in pending if p.instrument.symbol == symbol and p.side is Side.SELL), ZERO)
            qty = quantities.get(symbol, ZERO)
            worst_quantity = max(abs(qty + buys), abs(qty - sells))
            if worst_quantity == ZERO:
                projected[symbol] = ZERO
                continue
            if symbol not in marks or symbol not in instruments:
                return False, "missing mark for risk reservation"
            projected[symbol] = worst_quantity * marks[symbol] * instruments[symbol].multiplier
        projected_symbol = projected[order.instrument.symbol]
        if projected_symbol > self.limits.max_symbol_notional:
            return False, "max symbol notional exceeded"

        projected_gross = sum(projected.values(), ZERO)
        if projected_gross > self.limits.max_gross_notional:
            return False, "max gross notional exceeded"

        cutoff = order.created_ns - 1_000_000_000
        while self._recent_orders and self._recent_orders[0] <= cutoff:
            self._recent_orders.popleft()
        if len(self._recent_orders) >= self.limits.max_orders_per_second:
            return False, "order-rate limit exceeded"
        self._recent_orders.append(order.created_ns)
        return True, "approved"

    def reset_day(self, starting_equity: Decimal) -> None:
        self.starting_equity = starting_equity
        self.kill_switch = False
        self._recent_orders.clear()
