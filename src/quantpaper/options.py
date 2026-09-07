"""Black-Scholes reference pricing and Greeks for option sanity checks.

This is not an American-option production pricer. It is deliberately used only
to validate and generate research data; executable decisions use market quotes.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import erf, exp, log, pi, sqrt


def _cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def _pdf(value: float) -> float:
    return exp(-0.5 * value * value) / sqrt(2.0 * pi)


@dataclass(frozen=True, slots=True)
class OptionValue:
    price: float
    delta: float
    gamma: float
    vega: float


def black_scholes(
    spot: float,
    strike: float,
    years: float,
    volatility: float,
    rate: float = 0.0,
    dividend_yield: float = 0.0,
    is_call: bool = True,
) -> OptionValue:
    if min(spot, strike, years, volatility) <= 0:
        raise ValueError("spot, strike, years and volatility must be positive")
    root_t = sqrt(years)
    d1 = (
        log(spot / strike)
        + (rate - dividend_yield + 0.5 * volatility * volatility) * years
    ) / (volatility * root_t)
    d2 = d1 - volatility * root_t
    discounted_spot = spot * exp(-dividend_yield * years)
    discounted_strike = strike * exp(-rate * years)
    if is_call:
        price = discounted_spot * _cdf(d1) - discounted_strike * _cdf(d2)
        delta = exp(-dividend_yield * years) * _cdf(d1)
    else:
        price = discounted_strike * _cdf(-d2) - discounted_spot * _cdf(-d1)
        delta = exp(-dividend_yield * years) * (_cdf(d1) - 1.0)
    gamma = exp(-dividend_yield * years) * _pdf(d1) / (spot * volatility * root_t)
    vega = discounted_spot * _pdf(d1) * root_t
    return OptionValue(price=price, delta=delta, gamma=gamma, vega=vega)

