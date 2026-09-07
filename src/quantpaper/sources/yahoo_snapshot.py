"""Strict, observation-time Yahoo snapshots for isolated daily research refreshes.

These adjusted prices are known at the response observation time, not at their
historical bar dates.  Corporate actions and later provider revisions can change
old prices; this is not a historical point-in-time reconstruction.  The content
hash identifies the normalized snapshot, not raw HTTP response bytes.  The client
performs no persistent writes, model work or trading.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from numbers import Real
import os
import re
from tempfile import TemporaryDirectory
from typing import Any

import pandas as pd


_COLUMNS = ("open", "high", "low", "close", "volume")
_MAX_INPUT_ROWS = 1500
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.-]{0,14}\Z")


@dataclass(frozen=True, slots=True)
class YahooSnapshot:
    symbol: str
    observed_at: str
    frame: pd.DataFrame
    request: dict[str, object]
    content_hash: str
    excluded_incomplete_rows: int


def _default_history(symbol: str, **kwargs: Any) -> pd.DataFrame:
    # Use in a fresh, single-fetch CLI process: yfinance's public cache setter
    # must run before any ticker/cache use and is process-global.  Never persist
    # its cookie/ISIN/timezone caches with research artifacts or model inputs.
    with TemporaryDirectory(prefix="quantpaper-yahoo-") as cache_directory:
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with redirect_stdout(sink), redirect_stderr(sink):
                # Importing this module itself never imports a provider.
                import yfinance as yf

                yf.set_tz_cache_location(cache_directory)
                try:
                    return yf.Ticker(symbol).history(**kwargs)
                finally:
                    # The public setter closes all three cache DB managers.
                    # Keep the location task-owned until process exit.
                    yf.set_tz_cache_location(cache_directory)


def _utc_timestamp(value: object, message: str) -> pd.Timestamp:
    try:
        timestamp = pd.Timestamp(value)
        if pd.isna(timestamp) or timestamp.tzinfo is None:
            raise ValueError(message)
        return timestamp.tz_convert("UTC")
    except Exception:
        raise ValueError(message) from None


def _iso(timestamp: pd.Timestamp) -> str:
    return timestamp.isoformat().replace("+00:00", "Z")


class YahooSnapshotClient:
    """Fetch one bounded daily snapshot, with an injectable provider and clock.

    ``get_history`` accepts ``(symbol, **history_options)``.  ``now`` is a
    zero-argument aware-datetime clock called only after that provider returns.
    Missing and invalid rows are rejected rather than repaired or imputed.  A
    nonempty valid response containing only incomplete bars produces an empty
    snapshot with a nonzero exclusion count; an empty provider response fails.
    """

    def __init__(
        self,
        get_history: Callable[..., pd.DataFrame] | None = None,
        now: Callable[[], object] | None = None,
    ) -> None:
        self._get_history = _default_history if get_history is None else get_history
        self._now = (lambda: datetime.now(timezone.utc)) if now is None else now

    def fetch(self, symbol: str, period: str = "2y") -> YahooSnapshot:
        if not isinstance(symbol, str):
            raise ValueError("Invalid Yahoo research symbol")
        clean_symbol = symbol.strip().upper()
        if not _SYMBOL.fullmatch(clean_symbol) or ".." in clean_symbol:
            raise ValueError("Invalid Yahoo research symbol")
        if not isinstance(period, str) or period not in ("1y", "2y"):
            raise ValueError("Yahoo research period must be 1y or 2y")

        options = {
            "period": period,
            "interval": "1d",
            "auto_adjust": True,
            "back_adjust": False,
            "repair": False,
            "actions": False,
            "keepna": True,
            "prepost": False,
            "rounding": False,
            "timeout": 30,
            # Supported by the installed yfinance 1.7.0.  This avoids mutating
            # yfinance's process-global exception/logging configuration.
            "raise_errors": True,
        }
        try:
            source = self._get_history(clean_symbol, **options)
        except Exception:
            # Provider errors may contain URLs, cookies or other request data.
            raise ValueError("Yahoo snapshot request failed") from None
        try:
            observed = _utc_timestamp(self._now(), "Invalid Yahoo observation clock")
        except Exception:
            raise ValueError("Invalid Yahoo observation clock") from None
        if not isinstance(source, pd.DataFrame):
            raise ValueError("Yahoo snapshot response is not a data frame")
        if source.empty:
            raise ValueError("No Yahoo daily data returned")
        if len(source) > _MAX_INPUT_ROWS:
            raise ValueError("Yahoo snapshot exceeds the input row limit")
        if isinstance(source.columns, pd.MultiIndex):
            raise ValueError("Yahoo snapshot has invalid columns")
        names = [str(column).strip().lower().replace(" ", "_") for column in source.columns]
        if len(names) != len(set(names)) or not set(_COLUMNS).issubset(names):
            raise ValueError("Yahoo snapshot has missing or duplicate columns")

        output = source.copy(deep=True)
        output.columns = names
        output = output.loc[:, list(_COLUMNS)].copy()
        timestamps = [
            _utc_timestamp(value, "Yahoo snapshot has missing or naive timestamps")
            for value in output.index
        ]
        try:
            output.index = pd.DatetimeIndex(timestamps, name="timestamp")
            if output.index.has_duplicates:
                raise ValueError("Yahoo snapshot has duplicate timestamps")
            # Daily rows sharing a UTC date are not silently combined either.
            if output.index.normalize().has_duplicates:
                raise ValueError("Yahoo snapshot has duplicate daily bars")
        except ValueError:
            raise
        except Exception:
            raise ValueError("Yahoo snapshot has invalid timestamps") from None

        for row in output.itertuples(index=False, name=None):
            if any(
                isinstance(value, bool) or not isinstance(value, Real)
                or not math.isfinite(float(value))
                for value in row
            ):
                raise ValueError("Yahoo snapshot contains missing or nonfinite values")
            opened, high, low, closed, volume = (float(value) for value in row)
            if min(opened, high, low, closed) <= 0 or volume < 0:
                raise ValueError("Yahoo snapshot contains invalid prices or volume")
            if high < max(opened, low, closed) or low > min(opened, high, closed):
                raise ValueError("Yahoo snapshot violates OHLC bounds")

        try:
            complete = output.index <= observed - timedelta(days=1)
        except Exception:
            raise ValueError("Yahoo snapshot has invalid timestamps") from None
        excluded = int((~complete).sum())
        output = output.loc[complete].astype(float).sort_index()
        request: dict[str, object] = {
            "symbol": clean_symbol,
            **options,
            "availability_basis": "local_response_observed_adjusted_snapshot",
            "completion_policy": "event_time_plus_24h_at_or_before_observed_at",
        }
        rows = [
            {"timestamp": _iso(timestamp), **dict(zip(_COLUMNS, values, strict=True))}
            for timestamp, values in zip(
                output.index, output.itertuples(index=False, name=None), strict=True
            )
        ]
        canonical = json.dumps(
            {"request": request, "observed_at": _iso(observed), "rows": rows,
             "excluded_incomplete_rows": excluded},
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        return YahooSnapshot(
            symbol=clean_symbol, observed_at=_iso(observed), frame=output,
            request=request, content_hash=hashlib.sha256(canonical).hexdigest(),
            excluded_incomplete_rows=excluded,
        )
