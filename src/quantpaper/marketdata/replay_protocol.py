"""Frozen-study-derived quote replay windows, not an executable order plan.

Input provenance is verified by the caller. This module additionally rebuilds
the original bounded study schedule and requires an exact match of every core
field. Replaying event timestamps is not recovery of historical receive times.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math

from .study_protocol import build_study_plan


_CORE_FIELDS = (
    "contract", "created_at", "cutoff", "research_only", "execution_enabled",
    "sessions_per_asset", "symbols", "stock_feed", "crypto_feed", "observations",
    "timing", "limits", "summary",
)
_WINDOW_NAMES = ("opening", "midday", "closing")


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_safe(value) -> None:
    """Reject non-JSON objects, non-string keys, and nonfinite extra metadata."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("source study must contain only finite JSON values")
        return
    if type(value) is list:
        for item in value:
            _json_safe(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_safe(item)
        return
    raise ValueError("source study must contain only JSON-safe values and string keys")


def _canonical(value) -> str:
    # JSON equality distinguishes True from 1 and integral floats from integers.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _parse_utc(value, *, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an aware UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an aware UTC timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be an aware UTC timestamp")
    return parsed.astimezone(timezone.utc)


def build_replay_plan(study_plan: dict, *, now: datetime) -> dict:
    """Derive fixed seven-second quote windows from a verified frozen study.

    No date, feed, age, latency or universe override is exposed. A missing or
    unusable warmup quote remains unavailable; it cannot be repaired with a
    quote from after the hypothetical as-of time.
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("replay now must be a timezone-aware datetime")
    current = now.astimezone(timezone.utc)
    if type(study_plan) is not dict:
        raise ValueError("source study must be a JSON object")
    try:
        _json_safe(study_plan)
    except RecursionError:
        raise ValueError("source study JSON nesting is excessive or cyclic") from None
    missing = [name for name in _CORE_FIELDS if name not in study_plan]
    if missing:
        raise ValueError("source study is missing required core fields: " + ", ".join(missing))
    if study_plan["contract"] != "intraday_multi_session_v1":
        raise ValueError("source study contract must be intraday_multi_session_v1")
    original_created = _parse_utc(study_plan["created_at"], name="source study created_at")
    if original_created > current:
        raise ValueError("source study cannot be created in the future relative to replay now")
    symbols = study_plan["symbols"]
    if type(symbols) is not list:
        raise ValueError("source study symbols must be a JSON list")
    expected = build_study_plan(
        now=original_created, sessions=study_plan["sessions_per_asset"],
        stock_feed=study_plan["stock_feed"], symbols=tuple(symbols),
    )
    for name in _CORE_FIELDS:
        if _canonical(study_plan[name]) != _canonical(expected[name]):
            raise ValueError(f"source study core field does not match reconstructed protocol: {name}")

    windows = []
    completion_cutoff = current - timedelta(minutes=30)
    for observation in expected["observations"]:
        session_start = _parse_utc(observation["session_start"], name="session_start")
        session_end = _parse_utc(observation["session_end"], name="session_end")
        if session_end > completion_cutoff:
            raise ValueError("replay source sessions must be complete with a 30-minute buffer")
        for window_name in _WINDOW_NAMES:
            target = _parse_utc(observation["targets"][window_name], name="target")
            start = target - timedelta(seconds=5)
            end = target + timedelta(seconds=2)
            if not session_start <= start < target < end <= session_end:
                raise ValueError("replay capture windows must lie wholly inside their source session")
            if end > current:
                raise ValueError("replay capture windows cannot extend into the future")
            windows.append({
                "symbol": observation["symbol"], "session": observation["session"],
                "window_name": window_name, "target": _stamp(target),
                "feed": observation["feed"], "start": _stamp(start), "end": _stamp(end),
            })
    if not 1 <= len(windows) <= 180:
        raise ValueError("replay capture window budget exceeded")
    return {
        "contract": "intraday_asof_replay_v1", "created_at": _stamp(current),
        "research_only": True, "execution_enabled": False,
        "source_study": {
            name: expected[name] for name in (
                "contract", "created_at", "sessions_per_asset", "symbols", "stock_feed", "crypto_feed",
            )
        } | {"sample_days_by_asset": expected["summary"]["sample_days_by_asset"]},
        "windows": windows,
        "policy": {
            "warmup_seconds": 5, "tail_seconds": 2, "max_age_ms": 1000,
            "latency_scenarios_ms": [0, 250, 1000],
            "quote_time_rule": "quote.t < asof", "equal_asof_excluded": True,
            "invalid_updates_poison_state": True,
            "stock_quote_conditions": ["R"], "stock_tapes": ["A", "B", "C"],
            "crypto_stock_conditions_applied": False,
            "locked_quotes_allowed": False, "crossed_quotes_allowed": False,
            "nonpositive_prices_or_sizes_allowed": False,
            "max_age_scope": "fixed research assumption, not empirically calibrated",
        },
        "limits": {"max_windows": 180, "max_pages_per_fetch": 3, "max_pages": 540},
        "summary": {
            "expected_windows": len(windows), "expected_request_segments": len(windows),
            "max_planned_pages": len(windows) * 3,
        },
        "http_scope": "GET data.alpaca.markets historical quotes only; no bars, account or order endpoints",
        "interpretation": [
            "The source study must be integrity-verified by the caller; protocol reconstruction alone is not source authentication.",
            "The same original study targets and feeds are reused without outcome-dependent date, symbol or window selection.",
            "Capture uses [target-5 seconds, target+2 seconds]; replay may only consume quote.t strictly before each asof.",
            "A quote at the exact asof time is excluded because timestamp equality does not establish causality.",
            "A prior invalid quote update poisons state until a later valid update; an older valid price must not be reused across it.",
            "Stock quotes require exactly condition [R] and tape A/B/C; these stock condition/tape filters do not apply to crypto.",
            "Locked, crossed, nonpositive or nonfinite prices/sizes are unusable; filtering does not establish trading-status or fill eligibility.",
            "Maximum quote age is fixed at 1000 ms for this research protocol, not estimated or proven suitable for execution.",
            "Five seconds of warmup may be insufficient; missing, stale or invalid state remains unavailable, never future-backfilled.",
            "Latency scenarios are 0, 250 and 1000 ms event-time shifts, not measured receive latency or order simulation.",
            "Historical backfill does not establish data available at the original decision time or realtime SIP entitlement.",
            "No fills, fees, capacity, queue position, investment returns or strategy performance are inferred.",
        ],
    }
