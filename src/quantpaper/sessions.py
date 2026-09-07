"""Explicit exchange session boundaries for daily research signals.

XNYS handles US holidays, early closes and DST. Completion has a 30-minute
publication buffer; a wall-clock close alone is not proof of data finality.
"""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache
import warnings

import exchange_calendars as xcals
import numpy as np
import pandas as pd


def utc_timestamp(value=None) -> pd.Timestamp:
    stamp = pd.Timestamp.now(tz="UTC") if value is None else pd.Timestamp(value)
    if stamp.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return stamp.tz_convert("UTC").as_unit("ns")


@lru_cache(maxsize=8)
def equity_calendar(start: str | None = None, end: str | None = None):
    """Return XNYS with optional explicit historical coverage.

    The library's default window is suitable for a live clock, but not for a
    max-history download: callers filtering historical bars must give bounds.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="The 'generic' unit.*", category=DeprecationWarning)
        return xcals.get_calendar("XNYS", start=start, end=end)


def is_crypto(symbol: str) -> bool:
    return symbol.upper().endswith("-USD")


def session_bounds(symbol: str, session) -> tuple[pd.Timestamp, pd.Timestamp]:
    day = pd.Timestamp(session).date().isoformat()
    if is_crypto(symbol):
        start = pd.Timestamp(day, tz="UTC")
        return start, start + pd.Timedelta(days=1)
    calendar = equity_calendar()
    if not calendar.is_session(day):
        raise ValueError(f"{day} is not an XNYS session")
    return calendar.session_open(day), calendar.session_close(day)


def next_session(symbol: str, feature_date) -> str:
    day = pd.Timestamp(feature_date).date()
    if is_crypto(symbol):
        return (day + timedelta(days=1)).isoformat()
    calendar = equity_calendar()
    if not calendar.is_session(day.isoformat()):
        raise ValueError(f"feature date {day} is not an XNYS session")
    return calendar.next_session(day.isoformat()).date().isoformat()


def completed_bars(frame: pd.DataFrame, symbol: str, now=None) -> pd.DataFrame:
    current = utc_timestamp(now)
    data = frame.copy()
    data.index = pd.to_datetime(data.index, utc=True).normalize()
    if data.index.has_duplicates or data.index.hasnans:
        raise ValueError(f"duplicate or invalid daily bar dates for {symbol}")
    data = data.sort_index()
    if data.empty:
        return data
    if is_crypto(symbol):
        ready = data.index + pd.Timedelta(days=1, minutes=30) <= current
    else:
        # Include padding so weekend-only inputs still create a valid calendar.
        # Do not let the library's moving default window silently erase old bars.
        calendar = equity_calendar(
            start=(data.index.min().date() - timedelta(days=14)).isoformat(),
            end=(data.index.max().date() + timedelta(days=14)).isoformat(),
        )
        # Vectorized schedule lookup avoids thousands of Python calendar calls.
        labels = data.index.tz_localize(None)
        schedule = calendar.schedule.reindex(labels)
        closes = pd.to_datetime(schedule["close"], utc=True).astype("datetime64[ns, UTC]")
        ready = (closes + pd.Timedelta(minutes=30) <= current).to_numpy()
    return data.loc[np.asarray(ready)].copy()


def validate_bar(row: pd.Series) -> None:
    prices = np.array([row[name] for name in ("open", "high", "low", "close")], dtype=float)
    if not np.isfinite(prices).all() or (prices <= 0).any():
        raise ValueError("OHLC must be finite and positive")
    opening, high, low, closing = prices
    tolerance = max(prices) * 1e-12
    if high + tolerance < max(opening, closing) or low - tolerance > min(opening, closing) or low > high:
        raise ValueError("inconsistent OHLC range")
