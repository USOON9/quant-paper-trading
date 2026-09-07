"""Read-only checks of observed minute bars and displayed quote spreads.

This is a market-data observation audit. It has no strategy, broker, training,
or persistence interface. A displayed quote is not a promised fill, and raw
displayed size is not an estimate of executable capacity.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
import math
from numbers import Real

import numpy as np
import pandas as pd


_MINUTE_NS = 60 * 1_000_000_000


def _timestamp(value) -> pd.Timestamp:
    if not isinstance(value, (str, datetime, pd.Timestamp)):
        raise ValueError("invalid_timestamp")
    try:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp) or stamp.tzinfo is None:
            raise ValueError("timezone_required")
        return stamp.tz_convert("UTC").as_unit("ns")
    except (ValueError, TypeError, OverflowError):
        raise ValueError("invalid_or_naive_timestamp") from None


def _number(value, *, positive=False) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("nonnumeric_value")
    result = float(value)
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        raise ValueError("nonfinite_or_invalid_value")
    return result


def _records(fetch: dict) -> list:
    if not isinstance(fetch, dict) or not isinstance(fetch.get("records"), list):
        raise ValueError("each fetch must contain a records list")
    return fetch["records"]


def _optional_timestamp(value) -> pd.Timestamp | None:
    try:
        return _timestamp(value)
    except ValueError:
        return None


def _fetch_info(fetch: dict) -> dict:
    info = {"complete": fetch.get("complete") is True,
            "feed": fetch.get("feed") if isinstance(fetch.get("feed"), str) else None,
            "symbol": fetch.get("symbol") if isinstance(fetch.get("symbol"), str) else None,
            "kind": fetch.get("kind") if isinstance(fetch.get("kind"), str) else None,
            "pages": fetch.get("pages") if type(fetch.get("pages")) is int else None}
    for key in ("start", "end", "observed_at"):
        parsed = _optional_timestamp(fetch.get(key))
        info[key] = parsed.isoformat() if parsed is not None else None
    return info


def _source_metadata_reasons(
    name: str, fetch: dict, symbol: str, kind: str,
    required_start: pd.Timestamp, required_end: pd.Timestamp,
) -> list[str]:
    reasons = []
    if "symbol" in fetch and fetch["symbol"] != symbol:
        reasons.append(f"{name}_declares_wrong_symbol")
    if "kind" in fetch and fetch["kind"] != kind:
        reasons.append(f"{name}_declares_wrong_record_kind")
    if any(isinstance(record, dict) and any(
        field in record and record[field] != symbol for field in ("symbol", "S")
    ) for record in _records(fetch)):
        reasons.append(f"{name}_record_declares_wrong_symbol")
    start = _optional_timestamp(fetch.get("start"))
    end = _optional_timestamp(fetch.get("end"))
    observed = _optional_timestamp(fetch.get("observed_at"))
    if (start is None or end is None or start >= end
            or start > required_start or end < required_end):
        reasons.append(f"{name}_fetch_range_does_not_cover_required_window")
    if observed is None or (end is not None and observed < end):
        reasons.append(f"{name}_observation_time_missing_or_before_window_end")
    return reasons


def _bar_quality(fetch: dict, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    records = _records(fetch)
    by_time: dict[pd.Timestamp, dict[tuple, int]] = {}
    errors: Counter = Counter()
    outside = duplicates = 0
    for record in records:
        try:
            if not isinstance(record, dict):
                raise ValueError("invalid_record")
            stamp = _timestamp(record.get("t"))
            if not start <= stamp < end:
                outside += 1
                continue
            if stamp.value % _MINUTE_NS:
                raise ValueError("not_exact_minute")
            opening, high, low, closing = [_number(record.get(key), positive=True) for key in ("o", "h", "l", "c")]
            volume = _number(record.get("v"))
            tolerance = max(opening, high, low, closing) * 1e-12
            if high + tolerance < max(opening, low, closing) or low - tolerance > min(opening, high, closing):
                raise ValueError("invalid_ohlc_bounds")
            trades = _number(record["n"]) if "n" in record else None
            if trades is not None and not trades.is_integer():
                raise ValueError("noninteger_trade_count")
            vwap = _number(record["vw"]) if "vw" in record else None
            signature = (opening, high, low, closing, volume, trades, vwap)
            bucket = by_time.setdefault(stamp, {})
            if signature in bucket:
                duplicates += 1
            bucket[signature] = bucket.get(signature, 0) + 1
        except ValueError as error:
            errors[str(error)] += 1
    conflicts = {stamp: bucket for stamp, bucket in by_time.items() if len(bucket) > 1}
    valid = {stamp: next(iter(bucket)) for stamp, bucket in by_time.items() if len(bucket) == 1}
    expected = pd.date_range(start, end, freq="min", inclusive="left")
    missing = expected.difference(pd.DatetimeIndex(list(valid)))
    conflicting_records = sum(sum(bucket.values()) for bucket in conflicts.values())
    return {
        "fetch": _fetch_info(fetch), "range_convention": "session_start_inclusive_session_end_exclusive",
        "received_count": len(records), "expected_minutes": len(expected),
        "valid_count": len(valid), "missing_count": len(missing),
        "coverage_ratio": len(valid) / len(expected),
        "zero_volume_count": sum(values[4] == 0 for values in valid.values()),
        "invalid_count": sum(errors.values()) + conflicting_records,
        "invalid_record_reasons": dict(sorted(errors.items())),
        "outside_session_count": outside, "identical_duplicate_count": duplicates,
        "conflicting_minute_count": len(conflicts), "conflicting_record_count": conflicting_records,
        "conflicting_minutes": [stamp.isoformat() for stamp in sorted(conflicts)],
        "missing_minutes": [stamp.isoformat() for stamp in missing],
        "missing_bar_policy": "missing or conflicting minutes stay missing; no synthetic fill or forward fill",
    }


def _spread_stats(quotes: list[dict]) -> dict:
    values = np.asarray([quote["full_spread_bps"] for quote in quotes], dtype=float)
    result = {"weighting": "quote_event_weighted_not_time_weighted", "count": len(values)}
    for label in ("min", "median", "mean", "p90", "p95", "max"):
        result[label] = None
    if len(values):
        result.update(min=float(values.min()), median=float(np.median(values)),
                      mean=float(values.mean()), p90=float(np.quantile(values, 0.9)),
                      p95=float(np.quantile(values, 0.95)), max=float(values.max()))
    return result


def _quote_window(fetch: dict, target: pd.Timestamp, wait: int) -> dict:
    records = _records(fetch)
    deadline = target + pd.Timedelta(seconds=wait)
    request_start = _optional_timestamp(fetch.get("start"))
    request_end = _optional_timestamp(fetch.get("end"))
    errors: Counter = Counter()
    unique = set()
    quotes = []
    duplicates = outside = before = after = 0
    for record in records:
        try:
            if not isinstance(record, dict):
                raise ValueError("invalid_record")
            stamp = _timestamp(record.get("t"))
            before += stamp < target
            after += stamp > deadline
            if ((request_start is not None and stamp < request_start)
                    or (request_end is not None and stamp >= request_end)):
                outside += 1
                continue
            bid, ask, bid_size, ask_size = [_number(record.get(key), positive=True) for key in ("bp", "ap", "bs", "as")]
            if bid > ask:
                raise ValueError("crossed_quote")
            midpoint = bid + (ask - bid) / 2
            spread = (ask - bid) / midpoint * 10_000
            # Normalize only the timestamp; all other fields participate in exact
            # duplicate identity. Distinct updates at one timestamp are retained.
            fingerprint = json.dumps({**record, "t": stamp.isoformat()}, sort_keys=True, allow_nan=False)
            if fingerprint in unique:
                duplicates += 1
                continue
            unique.add(fingerprint)
            quotes.append({
                "timestamp": stamp.isoformat(), "bid_price": bid, "ask_price": ask,
                "bid_size_raw_units": bid_size, "ask_size_raw_units": ask_size,
                "bid_exchange": str(record["bx"]) if "bx" in record else None,
                "ask_exchange": str(record["ax"]) if "ax" in record else None,
                "mid_price": midpoint, "full_spread_bps": spread,
                "delay_seconds": (stamp.value - target.value) / 1_000_000_000,
            })
        except (ValueError, TypeError, OverflowError) as error:
            reason = str(error) if isinstance(error, ValueError) and str(error) in {
                "invalid_record", "invalid_or_naive_timestamp", "invalid_timestamp",
                "nonnumeric_value", "nonfinite_or_invalid_value", "crossed_quote",
            } else "invalid_quote_record"
            errors[reason] += 1
    quotes.sort(key=lambda quote: _timestamp(quote["timestamp"]).value)
    timely = [quote for quote in quotes if 0 <= quote["delay_seconds"] <= wait]
    first_stamp = timely[0]["timestamp"] if timely else None
    earliest = [quote for quote in timely if quote["timestamp"] == first_stamp]
    ambiguous = len(earliest) > 1
    return {
        "fetch": _fetch_info(fetch), "target_at": target.isoformat(),
        "deadline_inclusive": deadline.isoformat(), "max_quote_wait_seconds": wait,
        "received_count": len(records), "valid_count": len(quotes),
        "timely_valid_quote_count": len(timely), "invalid_count": sum(errors.values()),
        "invalid_record_reasons": dict(sorted(errors.items())),
        "identical_duplicate_count": duplicates, "outside_fetch_window_count": outside,
        "before_target_count": int(before), "after_deadline_count": int(after),
        "first_valid_timestamp": first_stamp, "first_timestamp_quote_count": len(earliest),
        "ambiguous_first_quote": ambiguous,
        "selected_quote": earliest[0] if len(earliest) == 1 else None,
        "spread_bps_distribution": _spread_stats(quotes),
        "selection_policy": "first valid timestamp at or after target and at most the wait limit; unsequenced ties are unusable",
        "displayed_size_policy": "raw vendor units, diagnostic only; not executable capacity",
    }


def analyze_symbol(
    symbol: str, bars: dict, entry_quotes: dict, exit_quotes: dict, *,
    session_start: datetime, session_end: datetime, entry_at: datetime, exit_at: datetime,
    max_quote_wait_seconds: int = 30,
) -> dict:
    """Return JSON-safe bar quality, quote-window distributions and touch costs.

    All three fetches must be complete and have the same recognized feed for a
    usable quote pair. ``crypto_us`` is recognized only for ``BTC/USD``; IEX
    and SIP are recognized only for stock symbols. Quotes at session end are
    not minute bars inside the half-open session. Quote statistics cover each
    requested fetch window; the selected quote additionally meets the wait cap.
    """
    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("symbol must be nonempty")
    start, end, entry, exit_ = map(_timestamp, (session_start, session_end, entry_at, exit_at))
    if (start.value % _MINUTE_NS or end.value % _MINUTE_NS
            or not start <= entry < exit_ <= end or start >= end):
        raise ValueError("session bounds must be exact minutes and contain ordered entry/exit targets")
    if type(max_quote_wait_seconds) is not int or max_quote_wait_seconds < 0:
        raise ValueError("max_quote_wait_seconds must be a nonnegative integer")
    bar_report = _bar_quality(bars, start, end)
    entry_report = _quote_window(entry_quotes, entry, max_quote_wait_seconds)
    exit_report = _quote_window(exit_quotes, exit_, max_quote_wait_seconds)
    reasons = []
    fetches = {"bars": bars, "entry_quotes": entry_quotes, "exit_quotes": exit_quotes}
    complete = all(fetch.get("complete") is True for fetch in fetches.values())
    for name, fetch in fetches.items():
        if fetch.get("complete") is not True:
            reasons.append(f"{name}_fetch_incomplete")
    # A truncated requested interval cannot establish the first quote in the
    # full selection window, even when pagination for that interval completed.
    inclusive_wait = pd.Timedelta(seconds=max_quote_wait_seconds, nanoseconds=1)
    metadata_reasons = (
        _source_metadata_reasons("bars", bars, symbol, "bars", start, end)
        + _source_metadata_reasons("entry_quotes", entry_quotes, symbol, "quotes", entry, entry + inclusive_wait)
        + _source_metadata_reasons("exit_quotes", exit_quotes, symbol, "quotes", exit_, exit_ + inclusive_wait)
    )
    reasons.extend(metadata_reasons)
    feeds = [fetch.get("feed") for fetch in fetches.values()]
    allowed = {"crypto_us"} if symbol == "BTC/USD" else (
        set() if "/" in symbol or symbol.endswith("-USD") else {"iex", "sip"}
    )
    matching = all(isinstance(feed, str) and feed in allowed for feed in feeds)
    if not matching:
        reasons.append("unknown_or_unsupported_feed_for_symbol")
    same_feed = all(feed == feeds[0] for feed in feeds)
    if not same_feed:
        reasons.append("cross_feed_quote_pair_forbidden")
    feed = feeds[0] if matching and same_feed else None
    feed_scope = {
        "iex": "IEX exchange only; not NBBO",
        "sip": "consolidated SIP feed; displayed touch is not a guaranteed fill",
        "crypto_us": "Alpaca US crypto venue feed; not stock NBBO or a universal crypto best quote",
    }.get(feed, "unverified feed scope")
    for name, window in (("entry", entry_report), ("exit", exit_report)):
        if window["ambiguous_first_quote"]:
            reasons.append(f"{name}_first_quote_ambiguous_without_sequence")
        elif window["selected_quote"] is None:
            reasons.append(f"{name}_has_no_valid_timely_quote")
    if bar_report["invalid_count"]:
        reasons.append("bar_quality_contains_invalid_or_conflicting_records")
    if bar_report["missing_count"]:
        reasons.append("bar_quality_has_missing_minutes")

    pair = None
    scenarios = []
    if (complete and not metadata_reasons and matching and same_feed
            and entry_report["selected_quote"] is not None and exit_report["selected_quote"] is not None):
        selected_entry, selected_exit = entry_report["selected_quote"], exit_report["selected_quote"]
        gross = selected_exit["mid_price"] / selected_entry["mid_price"] - 1
        touch = selected_exit["bid_price"] / selected_entry["ask_price"] - 1
        drag = (gross - touch) * 10_000
        approximate_spread = (selected_entry["full_spread_bps"] + selected_exit["full_spread_bps"]) / 2
        if all(math.isfinite(value) for value in (gross, touch, drag, approximate_spread)):
            pair = {
                "entry_quote_at": selected_entry["timestamp"], "exit_quote_at": selected_exit["timestamp"],
                "approximate_round_trip_spread_bps": approximate_spread,
                "gross_mid_return": gross, "touch_long_return": touch, "spread_drag_bps": drag,
                "interpretation": "observed quote-price comparison only; not strategy PnL, fills, or total trading cost",
            }
            scenarios = [{"assumed_round_trip_fee_bps": fee,
                          "spread_plus_assumed_fee_bps": approximate_spread + fee,
                          "interpretation": "fee stress scenario only, not actual broker fees"}
                         for fee in (0, 5, 10, 25, 50)]
        else:
            reasons.append("quote_pair_return_not_finite")
    return {
        "symbol": symbol, "research_only": True, "executable_backtest": False,
        "execution_enabled": False, "complete": complete, "feed": feed, "feed_scope": feed_scope,
        "source_metadata_valid": not metadata_reasons,
        "reasons": reasons, "bar_quality": bar_report,
        "entry_window": entry_report, "exit_window": exit_report,
        "cost_pair": pair, "fee_stress_scenarios": scenarios,
        "limitations": [
            "This execution-data audit does not alter or evaluate v3 strategy targets.",
            "Missing minute bars are not evidence of no trading, and have not been fabricated.",
            "Quote spreads are event-weighted over each fetched window, not time-weighted.",
            "Displayed quotes and sizes do not verify fills, latency, market impact, fees, or capacity.",
        ],
    }
