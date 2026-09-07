"""Session-aware and single-instance controls for the daily shadow cycle."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import os
from pathlib import Path
from typing import Iterator
from dotenv import dotenv_values
from .sessions import equity_calendar, next_session, session_bounds, utc_timestamp


def require_paper_gates_closed(env_path: Path) -> None:
    values = dotenv_values(env_path)
    unsafe = [
        name
        for name in ("ENABLE_ALPACA_PAPER", "ENABLE_ALPACA_PAPER_ROUND_TRIP")
        if os.environ.get(name, values.get(name, "NO")) != "NO"
    ]
    if unsafe:
        raise RuntimeError(f"shadow scheduler requires closed order gates: {', '.join(unsafe)}")


def eligible_symbols(
    symbols: list[str], now: datetime | None = None, close_delay_minutes: int = 30
) -> list[str]:
    current = utc_timestamp(now)
    calendar = equity_calendar()
    previous = calendar.date_to_session(current.date().isoformat(), direction="previous")
    if current < calendar.session_close(previous) + timedelta(minutes=close_delay_minutes):
        previous = calendar.previous_session(previous)
    target = next_session("SPY", previous)
    opening, _ = session_bounds("SPY", target)
    # Catch up after sleep/holidays only before the pending target opens.
    # Contiguous crypto sessions need a separately trained delayed-entry target.
    if current >= opening - timedelta(minutes=1):
        return []
    return list(dict.fromkeys(s for s in symbols if not s.endswith("-USD")))


@contextmanager
def exclusive_cycle_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another shadow cycle is already running") from error
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
