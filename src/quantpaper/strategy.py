"""Inventory-aware short-horizon order-book imbalance strategy.

This is a research baseline, not a claim of persistent alpha. It deliberately
uses only information available at the quote timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import count

from .domain import Order, OrderType, Position, Quote, Side, ZERO


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    imbalance_threshold: Decimal
    momentum_weight: Decimal
    cooldown_ns: int


class ImbalanceStrategy:
    def __init__(self, config: StrategyConfig, max_positions: dict[str, Decimal]) -> None:
        self.config = config
        self.max_positions = max_positions
        self._last_mid: dict[str, Decimal] = {}
        self._last_order_ns: dict[str, int] = {}
        self._ids = count(1)

    def on_quote(self, quote: Quote, position: Position) -> Order | None:
        symbol = quote.instrument.symbol
        previous_mid = self._last_mid.get(symbol, quote.mid)
        momentum = (quote.mid - previous_mid) / previous_mid if previous_mid else ZERO
        self._last_mid[symbol] = quote.mid

        last_order_ns = self._last_order_ns.get(symbol, -10**30)
        if quote.ts_ns - last_order_ns < self.config.cooldown_ns:
            return None

        score = quote.imbalance + self.config.momentum_weight * momentum * Decimal("10000")
        if abs(score) < self.config.imbalance_threshold:
            return None

        max_position = self.max_positions[symbol]
        target = max_position if score > ZERO else -max_position
        difference = target - position.quantity
        if difference == ZERO:
            return None

        self._last_order_ns[symbol] = quote.ts_ns
        side = Side.BUY if difference > ZERO else Side.SELL
        return Order(
            order_id=f"local-{next(self._ids):012d}",
            instrument=quote.instrument,
            side=side,
            quantity=abs(difference),
            order_type=OrderType.MARKET,
            created_ns=quote.ts_ns,
        )

