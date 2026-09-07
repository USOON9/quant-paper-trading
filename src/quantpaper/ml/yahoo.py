"""Yahoo Finance downloader with validation and deterministic local caching."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yfinance as yf


REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


def completed_daily_rows(
    frame: pd.DataFrame, now: pd.Timestamp | None = None
) -> pd.DataFrame:
    """Conservatively exclude today's potentially incomplete Yahoo daily bar."""
    current = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if current.tzinfo is None:
        current = current.tz_localize("UTC")
    else:
        current = current.tz_convert("UTC")
    cutoff = current.normalize()
    dates = pd.to_datetime(frame.index, utc=True).normalize()
    return frame.loc[dates < cutoff].copy()


@dataclass(frozen=True, slots=True)
class DownloadResult:
    symbol: str
    path: Path
    rows: int
    first_date: str
    last_date: str


class YahooDailyData:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def cached(self, symbol: str) -> DownloadResult:
        """Describe an existing cache without refreshing or modifying it."""
        clean_symbol = symbol.strip().upper()
        if not clean_symbol or any(char in clean_symbol for char in ("/", "\\", "..")):
            raise ValueError(f"invalid Yahoo symbol: {symbol!r}")
        path = self.cache_dir / f"{clean_symbol.replace('^', 'INDEX_')}.csv"
        frame = completed_daily_rows(self.load(path))
        if frame.empty:
            raise ValueError(f"no completed cached daily bars for {clean_symbol}")
        return DownloadResult(clean_symbol, path, len(frame), frame.index.min().date().isoformat(),
                              frame.index.max().date().isoformat())

    def download(
        self, symbol: str, period: str = "max", *,
        include_current_completed: bool = False, now: pd.Timestamp | None = None,
    ) -> DownloadResult:
        clean_symbol = symbol.strip().upper()
        if not clean_symbol or any(char in clean_symbol for char in ("/", "\\", "..")):
            raise ValueError(f"invalid Yahoo symbol: {symbol!r}")

        frame = yf.Ticker(clean_symbol).history(
            period=period,
            interval="1d",
            auto_adjust=True,
            actions=False,
            repair=True,
            timeout=30,
        )
        if frame.empty:
            raise RuntimeError(f"Yahoo returned no daily data for {clean_symbol}")
        frame.columns = [str(column).strip().lower().replace(" ", "_") for column in frame.columns]
        missing = set(REQUIRED_COLUMNS) - set(frame.columns)
        if missing:
            raise RuntimeError(f"{clean_symbol} is missing columns: {sorted(missing)}")

        output = frame.loc[:, REQUIRED_COLUMNS].copy()
        output.index = pd.to_datetime(output.index, utc=True)
        output.index.name = "timestamp"
        output = output[~output.index.duplicated(keep="last")].sort_index()
        output = output.dropna(subset=list(REQUIRED_COLUMNS))
        if include_current_completed:
            from ..sessions import completed_bars

            output = completed_bars(output, clean_symbol, now)
        else:
            output = completed_daily_rows(output, now=now)
        if len(output) < 100:
            raise RuntimeError(f"{clean_symbol} has only {len(output)} valid rows")

        path = self.cache_dir / f"{clean_symbol.replace('^', 'INDEX_')}.csv"
        output.to_csv(path)
        return DownloadResult(
            symbol=clean_symbol,
            path=path,
            rows=len(output),
            first_date=output.index[0].date().isoformat(),
            last_date=output.index[-1].date().isoformat(),
        )

    @staticmethod
    def load(path: Path) -> pd.DataFrame:
        frame = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
        frame.index = pd.to_datetime(frame.index, utc=True)
        return frame.sort_index()


def dataset_fingerprint(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.name.encode())
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()
