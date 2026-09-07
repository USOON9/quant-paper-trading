"""Deterministic synthetic quote generator for repeatable integration tests."""

from __future__ import annotations

import random
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterator

from .domain import Instrument, Quote


def synthetic_quotes(
    instruments: list[Instrument], events_per_instrument: int, seed: int
) -> Iterator[Quote]:
    rng = random.Random(seed)
    mids = {
        instruments[0].symbol: Decimal("225"),
        instruments[1].symbol: Decimal("62000"),
        instruments[2].symbol: Decimal("5.50"),
    }
    spreads = {
        instruments[0].symbol: Decimal("0.02"),
        instruments[1].symbol: Decimal("8.00"),
        instruments[2].symbol: Decimal("0.04"),
    }
    previous_imbalance = {instrument.symbol: Decimal("0") for instrument in instruments}
    base_ns = 1_788_489_000_000_000_000

    for event_index in range(events_per_instrument):
        for instrument_index, instrument in enumerate(instruments):
            symbol = instrument.symbol
            noise = Decimal(str(rng.gauss(0, 0.35)))
            predictive_move = previous_imbalance[symbol] * spreads[symbol] * Decimal("0.85")
            mids[symbol] = max(
                instrument.tick_size,
                mids[symbol] + predictive_move + noise * spreads[symbol],
            )
            ticks = (mids[symbol] / instrument.tick_size).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
            mid = ticks * instrument.tick_size
            half_spread = spreads[symbol] / Decimal("2")
            bid = max(instrument.tick_size, mid - half_spread)
            ask = mid + half_spread

            bid_size = Decimal(str(rng.randint(1, 100)))
            ask_size = Decimal(str(rng.randint(1, 100)))
            previous_imbalance[symbol] = (bid_size - ask_size) / (bid_size + ask_size)
            yield Quote(
                ts_ns=base_ns + event_index * 10_000_000 + instrument_index * 1_000,
                instrument=instrument,
                bid=bid,
                ask=ask,
                bid_size=bid_size,
                ask_size=ask_size,
            )

