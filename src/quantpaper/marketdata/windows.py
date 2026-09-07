"""Small, explicitly completed capture windows, not a trading schedule."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..sessions import equity_calendar


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("capture timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def plan_windows(
    *, now: datetime, stock_session: str | None = None, crypto_session: str | None = None,
) -> dict:
    now = utc(now)
    cutoff = now - timedelta(minutes=30)
    stock_day = datetime.strptime(stock_session, "%Y-%m-%d").date() if stock_session else cutoff.date()
    calendar = equity_calendar(
        start=(stock_day - timedelta(days=20)).isoformat(),
        end=(stock_day + timedelta(days=10)).isoformat(),
    )
    if stock_session:
        if stock_day.isoformat() != stock_session or not calendar.is_session(stock_session):
            raise ValueError("stock_session must name an XNYS trading session")
        session = calendar.date_to_session(stock_session)
    else:
        session = calendar.date_to_session(stock_day.isoformat(), direction="previous")
        if calendar.session_close(session).to_pydatetime() > cutoff:
            session = calendar.previous_session(session)
    stock_open = calendar.session_open(session).to_pydatetime()
    stock_close = calendar.session_close(session).to_pydatetime()
    if stock_close > cutoff:
        raise ValueError("stock session must be complete with a 30-minute publication buffer")
    if crypto_session:
        crypto_open = datetime.strptime(crypto_session, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if crypto_open.date().isoformat() != crypto_session:
            raise ValueError("crypto_session must be a YYYY-MM-DD date")
    else:
        crypto_open = cutoff.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    crypto_close = crypto_open + timedelta(days=1)
    if crypto_close > cutoff:
        raise ValueError("crypto UTC day must be complete with a 30-minute publication buffer")
    return {
        "stocks": {
            "session": session.date().isoformat(), "session_start": stock_open,
            "session_end": stock_close, "entry_at": stock_open + timedelta(minutes=5),
            "exit_at": stock_close - timedelta(minutes=5),
        },
        "crypto": {
            "session": crypto_open.date().isoformat(), "session_start": crypto_open,
            "session_end": crypto_close, "entry_at": crypto_open + timedelta(minutes=35),
            "exit_at": crypto_close - timedelta(minutes=5),
        },
    }
