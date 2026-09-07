"""Deterministic, bounded historical sampling plans, never trading schedules.

The caller freezes this JSON-safe plan before fetching anything. Selecting the
latest completed sessions avoids partial days, but is not random sampling and
does not recover information available at a historical decision time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..sessions import equity_calendar
from .windows import plan_windows


SYMBOLS = ("SPY", "JPM", "XOM", "WMT", "JNJ", "BTC/USD")
_MAX_SESSIONS = 10
_FETCH_LEGS = 4
_MAX_PAGES_PER_FETCH = 3


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_study_plan(
    *, now: datetime, sessions: int = 5, stock_feed: str = "sip",
    symbols: tuple[str, ...] = SYMBOLS,
) -> dict:
    """Plan 1–10 completed sessions per asset and three fixed quote windows.

    Dates cannot be overridden by a caller. A 30-minute completion buffer is a
    conservative collection rule, not proof that a provider has finalized data.
    Exact symbols and explicit stock feeds prevent accidental source changes.
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("study now must be a timezone-aware datetime")
    if type(sessions) is not int or not 1 <= sessions <= _MAX_SESSIONS:
        raise ValueError("sessions must be an integer from 1 through 10")
    if not isinstance(stock_feed, str) or stock_feed not in {"sip", "iex"}:
        raise ValueError("stock feed must be explicitly sip or iex; no implicit fallback")
    if (not isinstance(symbols, tuple) or not symbols
            or any(not isinstance(symbol, str) or symbol not in SYMBOLS for symbol in symbols)
            or len(symbols) != len(set(symbols))):
        raise ValueError("study symbols must be a unique nonempty supported tuple")

    current = now.astimezone(timezone.utc)
    cutoff = current - timedelta(minutes=30)
    latest = plan_windows(now=current)
    latest_stock_day = latest["stocks"]["session"]
    stock_day = latest["stocks"]["session_start"].date()
    calendar = equity_calendar(
        start=(stock_day - timedelta(days=max(60, sessions * 10))).isoformat(),
        end=(stock_day + timedelta(days=10)).isoformat(),
    )
    stock_sessions = [calendar.date_to_session(latest_stock_day)]
    for _ in range(sessions - 1):
        stock_sessions.append(calendar.previous_session(stock_sessions[-1]))
    stock_sessions.reverse()
    stock_windows = []
    for session in stock_sessions:
        opening = calendar.session_open(session).to_pydatetime()
        closing = calendar.session_close(session).to_pydatetime()
        midpoint = (opening + (closing - opening) / 2).replace(second=0, microsecond=0)
        stock_windows.append({
            "session": session.date().isoformat(),
            "session_start": opening, "session_end": closing,
            "targets": {"opening": opening + timedelta(minutes=5),
                        "midday": midpoint, "closing": closing - timedelta(minutes=5)},
        })
    latest_crypto_open = latest["crypto"]["session_start"]
    crypto_windows = []
    for days_ago in reversed(range(sessions)):
        opening = latest_crypto_open - timedelta(days=days_ago)
        closing = opening + timedelta(days=1)
        crypto_windows.append({
            "session": opening.date().isoformat(),
            "session_start": opening, "session_end": closing,
            "targets": {"opening": opening + timedelta(minutes=35),
                        "midday": opening + timedelta(hours=12),
                        "closing": closing - timedelta(minutes=5)},
        })

    observations = []
    for symbol in symbols:
        is_crypto = symbol == "BTC/USD"
        for window in crypto_windows if is_crypto else stock_windows:
            if window["session_end"] > cutoff:
                raise ValueError("all study sessions must be complete with the publication buffer")
            observations.append({
                "symbol": symbol, "session": window["session"],
                "feed": "crypto_us" if is_crypto else stock_feed,
                "session_start": _stamp(window["session_start"]),
                "session_end": _stamp(window["session_end"]),
                "targets": {name: _stamp(stamp) for name, stamp in window["targets"].items()},
            })
    expected_segments = len(observations) * _FETCH_LEGS
    if expected_segments > len(SYMBOLS) * _MAX_SESSIONS * _FETCH_LEGS:
        raise ValueError("study request budget exceeded")
    return {
        "contract": "intraday_multi_session_v1",
        "created_at": _stamp(current), "cutoff": _stamp(cutoff),
        "research_only": True, "execution_enabled": False,
        "sessions_per_asset": sessions, "symbols": list(symbols),
        "stock_feed": stock_feed, "crypto_feed": "crypto_us",
        "observations": observations,
        "timing": {
            "publication_buffer_minutes": 30, "quote_window_seconds": 60,
            "max_quote_wait_seconds": 30, "latency_scenarios_ms": [0, 250, 1000],
        },
        "limits": {
            "max_symbols": len(SYMBOLS), "max_sessions_per_asset": _MAX_SESSIONS,
            "fetch_legs_per_observation": _FETCH_LEGS,
            "max_pages_per_fetch": _MAX_PAGES_PER_FETCH,
            "max_fetches": 240, "max_pages": 720,
        },
        "summary": {
            "expected_observations": len(observations),
            "expected_request_segments": expected_segments,
            "max_planned_pages": expected_segments * _MAX_PAGES_PER_FETCH,
            "sample_days_by_asset": {
                "stocks": [window["session"] for window in stock_windows]
                if any(symbol != "BTC/USD" for symbol in symbols) else [],
                "crypto": [window["session"] for window in crypto_windows]
                if "BTC/USD" in symbols else [],
            },
        },
        "interpretation": [
            "Historical GET requests only; successful historical SIP access does not establish realtime SIP entitlement.",
            "The latest completed sessions are a bounded convenience sample, not a representative historical sample.",
            "Backfilled observations first collected now are not point-in-time evidence for historical decisions.",
            "XNYS calendar defines stock sessions, including holidays, early closes and DST; crypto uses UTC days.",
            "Opening, midday and closing windows are execution-data diagnostics, not frozen model labels or trading signals.",
            "Latency scenarios shift quote-selection times; they do not establish order fills, queue priority or market impact.",
            "Quote windows are 60 seconds; each target or shifted target permits at most 30 seconds to find a valid quote.",
            "Pagination completion is not evidence of complete market coverage; no missing bars are filled.",
            "Stocks use the explicitly selected feed; crypto_us is Alpaca US venue data, not global consolidated crypto.",
        ],
    }
