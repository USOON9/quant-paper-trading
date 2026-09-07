"""Event loop wiring data, strategy, risk, execution and accounting together."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .audit import AuditTrail
from .domain import Fill, Instrument, OrderStatus, Portfolio, Quote
from .execution import PaperExchange
from .reporting import RunReport, build_report
from .risk import RiskEngine
from .strategy import ImbalanceStrategy


@dataclass(slots=True)
class TradingEngine:
    instruments: dict[str, Instrument]
    portfolio: Portfolio
    strategy: ImbalanceStrategy
    risk: RiskEngine
    exchange: PaperExchange
    audit: AuditTrail | None = None

    def run(self, quotes: Iterable[Quote]) -> RunReport:
        marks: dict[str, Decimal] = {}
        equity_curve: list[Decimal] = []
        fills: list[Fill] = []
        rejected = 0
        last_ts = 0

        for quote in quotes:
            if quote.ts_ns < last_ts:
                raise ValueError("event stream must be chronological")
            last_ts = quote.ts_ns
            marks[quote.instrument.symbol] = quote.mid
            new_fills = self.exchange.on_quote(quote)
            self._book(new_fills)
            fills.extend(new_fills)

            order = self.strategy.on_quote(
                quote, self.portfolio.position(quote.instrument.symbol)
            )
            if order is not None:
                approved, reason = self.risk.approve(
                    order, quote, self.portfolio, marks, self.instruments,
                    pending_orders=self.exchange.orders.values(),
                )
                if approved:
                    self.exchange.submit(order)
                    if self.audit:
                        self.audit.record(
                            "order_approved",
                            quote.ts_ns,
                            {
                                "order_id": order.order_id,
                                "symbol": order.instrument.symbol,
                                "side": order.side.value,
                                "quantity": order.quantity,
                            },
                        )
                else:
                    order.status = OrderStatus.REJECTED
                    order.reject_reason = reason
                    self.exchange.orders[order.order_id] = order
                    rejected += 1
                    if self.audit:
                        self.audit.record(
                            "order_rejected",
                            quote.ts_ns,
                            {"order_id": order.order_id, "reason": reason},
                        )
            equity_curve.append(self.portfolio.equity(marks, self.instruments))

        final_fills = self.exchange.flush(last_ts)
        self._book(final_fills)
        fills.extend(final_fills)
        equity_curve.append(self.portfolio.equity(marks, self.instruments))
        return build_report(
            starting_equity=self.portfolio.starting_cash,
            equity_curve=equity_curve,
            fills=len(fills),
            rejected_orders=rejected,
            fees_paid=self.portfolio.fees_paid,
        )

    def _book(self, fills: list[Fill]) -> None:
        for fill in fills:
            self.portfolio.apply_fill(fill)
            if self.audit:
                self.audit.record(
                    "fill",
                    fill.ts_ns,
                    {
                        "order_id": fill.order_id,
                        "symbol": fill.instrument.symbol,
                        "side": fill.side.value,
                        "quantity": fill.quantity,
                        "price": fill.price,
                        "commission": fill.commission,
                    },
                )
