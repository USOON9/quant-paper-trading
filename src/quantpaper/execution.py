"""Deterministic local paper venue with latency, slippage and commissions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .domain import AssetClass, Fill, Order, OrderStatus, OrderType, Quote, Side


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    latency_ns: int
    slippage_bps: Decimal

    def __post_init__(self) -> None:
        if self.latency_ns < 0 or not self.slippage_bps.is_finite() or not 0 <= self.slippage_bps < 10000:
            raise ValueError("latency and slippage must be finite and nonnegative")


class CommissionModel:
    def calculate(self, order: Order, price: Decimal) -> Decimal:
        if order.instrument.asset_class is AssetClass.OPTION:
            return max(Decimal("0.65") * order.quantity, Decimal("1.00"))
        if order.instrument.asset_class is AssetClass.CRYPTO:
            return order.quantity * price * Decimal("0.0015")
        return max(order.quantity * Decimal("0.0035"), Decimal("0.35"))


class PaperExchange:
    def __init__(self, config: ExecutionConfig) -> None:
        self.config = config
        self.commissions = CommissionModel()
        self.orders: dict[str, Order] = {}
        self._pending: list[tuple[int, Order]] = []
        self._quotes: dict[str, Quote] = {}

    def submit(self, order: Order) -> None:
        if order.order_id in self.orders or order.status is not OrderStatus.NEW:
            raise ValueError("duplicate order id or non-new order")
        order.status = OrderStatus.PENDING
        self.orders[order.order_id] = order
        self._pending.append((order.created_ns + self.config.latency_ns, order))

    def cancel_all(self) -> None:
        for order in self.orders.values():
            if order.status in (OrderStatus.NEW, OrderStatus.PENDING):
                order.status = OrderStatus.CANCELLED

    def on_quote(self, quote: Quote) -> list[Fill]:
        previous = self._quotes.get(quote.instrument.symbol)
        if previous is not None and quote.ts_ns < previous.ts_ns:
            raise ValueError("quotes must be chronological")
        self._quotes[quote.instrument.symbol] = quote
        fills: list[Fill] = []
        still_pending: list[tuple[int, Order]] = []
        liquidity = {Side.BUY: quote.ask_size, Side.SELL: quote.bid_size}
        for due_ns, order in self._pending:
            if order.status is not OrderStatus.PENDING:
                continue
            # An unrelated instrument update cannot supply executable liquidity
            # for this order. Wait for its own post-arrival quote.
            if (
                due_ns <= quote.ts_ns
                and order.instrument == quote.instrument
                and quote.ts_ns > order.created_ns
                and order.quantity <= liquidity[order.side]
            ):
                fill = self._try_fill(order, quote, quote.ts_ns)
                if fill is not None:
                    fills.append(fill)
                    liquidity[order.side] -= fill.quantity
                    continue
            if order.status is OrderStatus.PENDING:
                still_pending.append((due_ns, order))
        self._pending = still_pending
        return fills

    def flush(self, ts_ns: int) -> list[Fill]:
        """End the replay without manufacturing future quotes or fills."""
        self.cancel_all()
        self._pending.clear()
        return []

    def _try_fill(self, order: Order, quote: Quote, ts_ns: int) -> Fill | None:
        touch = quote.ask if order.side is Side.BUY else quote.bid
        if order.order_type is OrderType.LIMIT:
            assert order.limit_price is not None
            marketable = (
                order.limit_price >= quote.ask
                if order.side is Side.BUY
                else order.limit_price <= quote.bid
            )
            if not marketable:
                return None
            touch = min(touch, order.limit_price) if order.side is Side.BUY else max(touch, order.limit_price)

        slip = touch * self.config.slippage_bps / Decimal("10000")
        raw_price = touch + slip if order.side is Side.BUY else touch - slip
        rounding = ROUND_CEILING if order.side is Side.BUY else ROUND_FLOOR
        ticks = (raw_price / order.instrument.tick_size).quantize(Decimal("1"), rounding=rounding)
        price = ticks * order.instrument.tick_size
        if order.limit_price is not None:
            price = min(price, order.limit_price) if order.side is Side.BUY else max(price, order.limit_price)
        if price <= 0:
            return None
        commission = self.commissions.calculate(order, price)
        order.status = OrderStatus.FILLED
        return Fill(
            order_id=order.order_id,
            ts_ns=ts_ns,
            instrument=order.instrument,
            side=order.side,
            quantity=order.quantity,
            price=price,
            commission=commission,
        )
