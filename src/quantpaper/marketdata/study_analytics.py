"""Equal-session, read-only quote diagnostics; not fills or strategy results.

The existing pure quality helpers remain the single record-validation policy.
Study-level provenance is stricter: symbol/kind are mandatory and every quote
distribution uses exactly the planned 60-second interval. No broker, filesystem,
network, training, fee, or order interface exists here.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import math

import numpy as np
import pandas as pd

from .quality import (
    _fetch_info, _optional_timestamp, _quote_window,
    _source_metadata_reasons, _timestamp, analyze_symbol,
)


WINDOWS = ("opening", "midday", "closing")
LATENCIES_MS = (0, 250, 1000)
WAIT_SECONDS = 30
WINDOW_SECONDS = 60


def _metadata_reasons(name, fetch, symbol, kind, start, end):
    reasons = _source_metadata_reasons(name, fetch, symbol, kind, start, end)
    for field in ("symbol", "kind"):
        if field not in fetch:
            reasons.append(f"{name}_{field}_missing")
    if kind == "quotes" and (
        _optional_timestamp(fetch.get("start")) != start
        or _optional_timestamp(fetch.get("end")) != end
    ):
        reasons.append(f"{name}_fetch_range_is_not_exact_planned_60_seconds")
    return list(dict.fromkeys(reasons))


def _allowed_feeds(symbol):
    if symbol == "BTC/USD":
        return {"crypto_us"}
    if "/" in symbol or symbol.endswith("-USD"):
        return set()
    return {"sip", "iex"}


def _displacement(later, earlier):
    value = (later / earlier - 1) * 10_000
    return value if math.isfinite(value) else None


def _window(symbol, name, fetch, target, bar_feed):
    source_reasons = _metadata_reasons(
        name, fetch, symbol, "quotes", target,
        target + pd.Timedelta(seconds=WINDOW_SECONDS),
    )
    if fetch.get("complete") is not True:
        source_reasons.append("fetch_incomplete_or_truncated")
    if not isinstance(fetch.get("feed"), str) or fetch.get("feed") not in _allowed_feeds(symbol):
        source_reasons.append("unknown_or_unsupported_feed_for_symbol")
    if fetch.get("feed") != bar_feed:
        source_reasons.append("quote_feed_differs_from_bar_feed")
    quality = _quote_window(fetch, target, WAIT_SECONDS)
    source_usable = not source_reasons
    scenarios = []
    for latency in LATENCIES_MS:
        delayed_target = target + pd.Timedelta(milliseconds=latency)
        delayed = quality if latency == 0 else _quote_window(fetch, delayed_target, WAIT_SECONDS)
        reasons = list(source_reasons)
        if delayed["ambiguous_first_quote"]:
            reasons.append("first_quote_ambiguous_without_sequence")
        elif delayed["selected_quote"] is None:
            reasons.append("no_valid_timely_quote")
        selected = delayed["selected_quote"] if not reasons else None
        scenarios.append({
            "latency_ms": latency, "target_at": delayed_target.isoformat(),
            "deadline_inclusive": delayed["deadline_inclusive"],
            "available": selected is not None, "reasons": reasons,
            "selected_quote": selected,
            "full_spread_bps": selected["full_spread_bps"] if selected else None,
            "wait_after_shift_seconds": selected["delay_seconds"] if selected else None,
            "paired_with_zero_latency": False,
            "event_time_gap_ms_vs_zero": None,
            "ask_price_displacement_bps_vs_zero": None,
            "bid_price_displacement_bps_vs_zero": None,
        })
    zero = scenarios[0]["selected_quote"]
    for scenario in scenarios:
        selected = scenario["selected_quote"]
        if selected is not None and zero is not None:
            ask = _displacement(selected["ask_price"], zero["ask_price"])
            bid = _displacement(selected["bid_price"], zero["bid_price"])
            if ask is not None and bid is not None:
                scenario.update(paired_with_zero_latency=True,
                                event_time_gap_ms_vs_zero=(
                                    _timestamp(selected["timestamp"]).value
                                    - _timestamp(zero["timestamp"]).value) / 1_000_000,
                                ask_price_displacement_bps_vs_zero=ask,
                                bid_price_displacement_bps_vs_zero=bid)
            else:
                scenario["reasons"].append("price_displacement_not_finite")
        elif selected is not None:
            scenario["reasons"].append("zero_latency_baseline_unavailable")
    reasons = list(scenarios[0]["reasons"])
    if quality["invalid_count"]:
        reasons.append("window_contains_invalid_quotes_excluded_from_statistics")
    return {
        "target_at": target.isoformat(), "usable": scenarios[0]["available"],
        "source_metadata_valid": not _metadata_reasons(
            name, fetch, symbol, "quotes", target,
            target + pd.Timedelta(seconds=WINDOW_SECONDS),
        ),
        "source_usable": source_usable,
        "distribution_usable": source_usable and quality["valid_count"] > 0,
        "reasons": reasons, "quality": quality, "latency_scenarios": scenarios,
    }


def analyze_observation(
    symbol: str, bars_fetch: dict, quotes_fetches: dict, *,
    session_start: datetime, session_end: datetime, targets: dict,
) -> dict:
    """Analyze one planned symbol/session, including empty or failed fetches.

    Missing observations must remain rows; pagination completion is not data
    coverage. Latency is a shift of historical event time, not measured network
    latency. Distinct quotes at an unsequenced first timestamp are unavailable.
    """
    if set(quotes_fetches) != set(WINDOWS) or set(targets) != set(WINDOWS):
        raise ValueError("exactly opening, midday, closing windows are required")
    symbol = symbol.strip().upper()
    start, end = _timestamp(session_start), _timestamp(session_end)
    times = {name: _timestamp(targets[name]) for name in WINDOWS}
    if not start <= times["opening"] < times["midday"] < times["closing"] < end:
        raise ValueError("ordered opening, midday, closing targets must be inside session")
    if any(target + pd.Timedelta(seconds=WINDOW_SECONDS) > end for target in times.values()):
        raise ValueError("each 60-second quote window must fit inside the session")
    legacy = analyze_symbol(
        symbol, bars_fetch, quotes_fetches["opening"], quotes_fetches["closing"],
        session_start=start, session_end=end,
        entry_at=times["opening"], exit_at=times["closing"],
        max_quote_wait_seconds=WAIT_SECONDS,
    )
    bar_reasons = _metadata_reasons("bars", bars_fetch, symbol, "bars", start, end)
    bar_feed = bars_fetch.get("feed")
    feed_valid = isinstance(bar_feed, str) and bar_feed in _allowed_feeds(symbol)
    windows = {
        name: _window(symbol, name, quotes_fetches[name], times[name], bar_feed)
        for name in WINDOWS
    }
    pair = None
    if (legacy["cost_pair"] is not None and not bar_reasons
            and windows["opening"]["usable"] and windows["closing"]["usable"]):
        pair = {key: legacy["cost_pair"][key] for key in (
            "entry_quote_at", "exit_quote_at", "approximate_round_trip_spread_bps",
        )}
        pair["interpretation"] = "sum of two displayed half-spreads; not fills, fees, total costs, or strategy PnL"
    reasons = list(bar_reasons)
    if bars_fetch.get("complete") is not True:
        reasons.append("bars_fetch_incomplete_or_truncated")
    if not feed_valid:
        reasons.append("bar_feed_unknown_or_unsupported")
    if legacy["bar_quality"]["missing_count"]:
        reasons.append("bar_quality_has_missing_minutes")
    if legacy["bar_quality"]["invalid_count"]:
        reasons.append("bar_quality_contains_invalid_or_conflicting_records")
    reasons.extend(f"{name}:{reason}" for name, window in windows.items() for reason in window["reasons"])
    return {
        "schema_version": 1, "symbol": symbol, "session_date": start.date().isoformat(),
        "session_start": start.isoformat(), "session_end": end.isoformat(),
        "feed": bar_feed if feed_valid else None, "feed_scope": {
            "sip": "consolidated SIP feed; displayed touch is not a guaranteed fill",
            "iex": "IEX exchange only; not NBBO",
            "crypto_us": "Alpaca US crypto venue feed; not a universal crypto best quote",
        }.get(bar_feed if feed_valid else None, "unverified feed scope"),
        "research_only": True, "execution_enabled": False, "executable_backtest": False,
        "complete": bars_fetch.get("complete") is True and all(
            fetch.get("complete") is True for fetch in quotes_fetches.values()),
        "source_metadata_valid": not bar_reasons and all(
            window["source_metadata_valid"] for window in windows.values()),
        "bar_source_usable": not bar_reasons and feed_valid and bars_fetch.get("complete") is True,
        "metadata": {"bars": _fetch_info(bars_fetch), "quotes": {
            name: _fetch_info(quotes_fetches[name]) for name in WINDOWS}},
        "bar_quality": legacy["bar_quality"], "windows": windows,
        "opening_closing_spread_pair": pair, "reasons": reasons,
        "limitations": [
            "Historical event timestamps are not receive timestamps or measured network latency.",
            "The first update after a shifted target is not the as-of best quote at that instant; actual event gaps and waits are reported.",
            "Latency scenarios select the first unique valid quote at/after the shifted target, with a 30-second inclusive wait cap.",
            "A sampled quote is not evidence of order acceptance, fill probability, queue priority, impact, or executable size.",
            "Window distributions are quote-event-weighted within a fixed 60-second interval, not time-weighted.",
            "Missing and invalid bars stay missing; zero-volume crypto bars may be quote-derived.",
            "No fees, fills, investment returns, or strategy performance are estimated.",
        ],
    }


def _stats(values):
    values = [float(value) for value in values if value is not None and math.isfinite(value)]
    return {
        "count": len(values), "weighting": "one_observation_per_usable_session_equal_session_weight",
        "min": float(min(values)) if values else None,
        "median": float(np.quantile(values, .5)) if values else None,
        "p90": float(np.quantile(values, .9)) if values else None,
        "max": float(max(values)) if values else None,
        "absolute_max": float(max(abs(value) for value in values)) if values else None,
    }


def _aggregate_window(windows, *, pooling_allowed):
    selected = [window for window in windows if window["usable"]] if pooling_allowed else []
    distributions = [window for window in windows if window["distribution_usable"]] if pooling_allowed else []
    result = {
        "expected_samples": len(windows), "usable_selected_samples": len(selected),
        "usable_distribution_samples": len(distributions),
        "unusable_selected_samples": len(windows) - len(selected),
        "missing_window_count": sum(window["quality"]["received_count"] == 0 for window in windows),
        "incomplete_or_truncated_window_count": sum(not window["quality"]["fetch"]["complete"] for window in windows),
        "invalid_quote_window_count": sum(window["quality"]["invalid_count"] > 0 for window in windows),
        "invalid_quote_record_count": sum(window["quality"]["invalid_count"] for window in windows),
        "invalid_source_metadata_window_count": sum(not window["source_metadata_valid"] for window in windows),
        "unusable_source_window_count": sum(not window["source_usable"] for window in windows),
        "no_timely_valid_quote_window_count": sum(window["quality"]["timely_valid_quote_count"] == 0 for window in windows),
        "ambiguous_first_quote_window_count": sum(window["quality"]["ambiguous_first_quote"] for window in windows),
        "selected_full_spread_bps": _stats([window["quality"]["selected_quote"]["full_spread_bps"] for window in selected]),
        "daily_window_event_median_spread_bps": _stats([
            window["quality"]["spread_bps_distribution"]["median"] for window in distributions]),
        "reason_counts": dict(sorted(Counter(reason for window in windows for reason in set(window["reasons"])).items())),
        "latency_scenarios": [],
    }
    for latency in LATENCIES_MS:
        scenarios = [next(item for item in window["latency_scenarios"] if item["latency_ms"] == latency) for window in windows]
        available = [item for item in scenarios if item["available"]] if pooling_allowed else []
        paired = [item for item in available if item["paired_with_zero_latency"]]
        result["latency_scenarios"].append({
            "latency_ms": latency, "expected_samples": len(windows),
            "available_samples": len(available), "unavailable_samples": len(windows) - len(available),
            "paired_with_zero_samples": len(paired),
            "full_spread_bps": _stats([item["full_spread_bps"] for item in available]),
            "wait_after_shift_seconds": _stats([item["wait_after_shift_seconds"] for item in available]),
            "paired_full_spread_bps": _stats([item["full_spread_bps"] for item in paired]),
            "event_time_gap_ms_vs_zero": _stats([item["event_time_gap_ms_vs_zero"] for item in paired]),
            "ask_price_displacement_bps_vs_zero": _stats([item["ask_price_displacement_bps_vs_zero"] for item in paired]),
            "bid_price_displacement_bps_vs_zero": _stats([item["bid_price_displacement_bps_vs_zero"] for item in paired]),
            "reason_counts": dict(sorted(Counter(reason for item in scenarios for reason in set(item["reasons"])).items())),
        })
    return result


def aggregate_observations(rows: list[dict]) -> dict:
    """Aggregate planned rows without pooling symbols or raw quote events.

    A missing fetch must be represented by an analyzed failed/empty row. This
    function cannot infer sessions omitted by a caller. Duplicate symbol/session
    rows are rejected; mixed feeds disable pooled spread statistics rather than
    silently averaging incompatible sources. Counts still expose every row.
    """
    grouped = defaultdict(list)
    seen = set()
    for row in rows:
        key = (row["symbol"], row["session_date"])
        if key in seen:
            raise ValueError("duplicate symbol/session observation")
        seen.add(key)
        grouped[row["symbol"]].append(row)
    by_symbol = {}
    for symbol, observations in sorted(grouped.items()):
        feeds = sorted({row["feed"] for row in observations if row["feed"] is not None})
        pooling_allowed = len(feeds) <= 1
        trusted_bars = [row for row in observations if row["bar_source_usable"]] if pooling_allowed else []
        expected_minutes = sum(row["bar_quality"]["expected_minutes"] for row in observations)
        observed_minutes = sum(row["bar_quality"]["valid_count"] for row in observations)
        trusted_minutes = sum(row["bar_quality"]["valid_count"] for row in trusted_bars)
        pairs = [row["opening_closing_spread_pair"] for row in observations
                 if row["opening_closing_spread_pair"] is not None] if pooling_allowed else []
        by_symbol[symbol] = {
            "feed": feeds[0] if len(feeds) == 1 else None, "observed_feeds": feeds,
            "pooling_allowed": pooling_allowed,
            "reasons": [] if pooling_allowed else ["mixed_feeds_no_spread_or_coverage_aggregation"],
            "session_dates": sorted(row["session_date"] for row in observations),
            "expected_sessions": len(observations),
            "pagination_complete_sessions": sum(row["complete"] for row in observations),
            "usable_complete_sessions": sum(row["bar_source_usable"] and all(
                window["usable"] for window in row["windows"].values()) for row in observations) if pooling_allowed else 0,
            "all_minutes_present_sessions": sum(row["bar_source_usable"] and row["bar_quality"]["missing_count"] == 0 for row in observations) if pooling_allowed else 0,
            "minute_coverage": {
                "expected_minutes": expected_minutes, "observed_valid_minutes_unfiltered_for_source": observed_minutes,
                "source_usable_valid_minutes": trusted_minutes,
                "source_usable_sessions": len(trusted_bars),
                "source_usable_minutes_divided_by_all_expected": trusted_minutes / expected_minutes if expected_minutes else None,
                "daily_coverage_ratio": _stats([row["bar_quality"]["coverage_ratio"] for row in trusted_bars]),
                "observed_missing_minutes": sum(row["bar_quality"]["missing_count"] for row in observations),
                "observed_invalid_bar_records": sum(row["bar_quality"]["invalid_count"] for row in observations),
                "observed_zero_volume_minutes": sum(row["bar_quality"]["zero_volume_count"] for row in observations),
            },
            "opening_closing_spread_pair": {
                "expected_samples": len(observations), "usable_samples": len(pairs),
                "approximate_round_trip_spread_bps": _stats([pair["approximate_round_trip_spread_bps"] for pair in pairs]),
            },
            "by_window": {name: _aggregate_window([row["windows"][name] for row in observations],
                                                   pooling_allowed=pooling_allowed) for name in WINDOWS},
        }
    return {
        "schema_version": 1, "observation_count": len(rows), "by_symbol": by_symbol,
        "research_only": True, "execution_enabled": False, "executable_backtest": False,
        "expected_sample_scope": "all supplied planned rows including failed/empty fetches; omitted sessions cannot be inferred",
        "aggregation_policy": "separate symbol/window; equal session weight; never pool raw quote events across sessions or different feeds",
        "latency_policy": "first updates after historical event-time shifts, not as-of quotes, receive-time latency, or fill simulation; signed ask/bid displacements and actual event gaps are paired within the same session/window",
        "limitations": ["Five sessions are a small diagnostic sample, not a stable execution-cost model.",
                        "Spread and displayed price changes exclude unknown fees, impact, queueing, and unfilled orders.",
                        "Selection availability may differ across latency scenarios; compare paired counts before interpreting differences."],
    }
