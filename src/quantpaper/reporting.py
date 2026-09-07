"""Performance metrics and JSON-safe report formatting."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class RunReport:
    starting_equity: Decimal
    ending_equity: Decimal
    net_pnl: Decimal
    return_pct: Decimal
    max_drawdown_pct: Decimal
    fills: int
    rejected_orders: int
    fees_paid: Decimal

    def to_dict(self) -> dict[str, str | int]:
        return {
            key: (str(value.quantize(Decimal("0.0001"))) if isinstance(value, Decimal) else value)
            for key, value in asdict(self).items()
        }


def build_report(
    starting_equity: Decimal,
    equity_curve: list[Decimal],
    fills: int,
    rejected_orders: int,
    fees_paid: Decimal,
) -> RunReport:
    ending = equity_curve[-1] if equity_curve else starting_equity
    peak = starting_equity
    maximum_drawdown = Decimal("0")
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            maximum_drawdown = max(maximum_drawdown, (peak - equity) / peak)
    pnl = ending - starting_equity
    return RunReport(
        starting_equity=starting_equity,
        ending_equity=ending,
        net_pnl=pnl,
        return_pct=pnl / starting_equity * Decimal("100"),
        max_drawdown_pct=maximum_drawdown * Decimal("100"),
        fills=fills,
        rejected_orders=rejected_orders,
        fees_paid=fees_paid,
    )

