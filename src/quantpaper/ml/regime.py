"""Lagged, point-in-time market regime features from liquid Yahoo proxies."""

from __future__ import annotations

import numpy as np
import pandas as pd


CONTEXT_SYMBOLS = ("SPY", "QQQ", "IWM", "^VIX", "^TNX")
MAX_CONTEXT_AGE = pd.Timedelta(days=7)
REGIME_FEATURE_COLUMNS = [
    "market_return_1",
    "market_return_20",
    "market_volatility_20",
    "growth_relative_20",
    "small_relative_20",
    "vix_level",
    "vix_change_5",
    "rate_level",
    "rate_change_20",
]


def _close(frame: pd.DataFrame) -> pd.Series:
    series = frame["close"].astype(float).copy()
    series.index = pd.to_datetime(series.index, utc=True).normalize()
    if series.index.hasnans or series.index.has_duplicates:
        raise ValueError("context bars must have unique, valid daily dates")
    if not np.isfinite(series.to_numpy()).all() or (series <= 0).any():
        raise ValueError("context closes must be positive and finite")
    return series.sort_index()


def _raw_regime(context_frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    missing = set(CONTEXT_SYMBOLS) - set(context_frames)
    if missing:
        raise ValueError(f"missing market context symbols: {sorted(missing)}")

    spy = _close(context_frames["SPY"])
    qqq = _close(context_frames["QQQ"])
    iwm = _close(context_frames["IWM"])
    vix = _close(context_frames["^VIX"])
    rates = _close(context_frames["^TNX"])

    equity = pd.concat({"spy": spy, "qqq": qqq, "iwm": iwm}, axis=1).dropna()
    regime = pd.DataFrame(index=equity.index)
    spy_returns = equity["spy"].pct_change(fill_method=None)
    regime["market_return_1"] = spy_returns
    regime["market_return_20"] = equity["spy"].pct_change(20, fill_method=None)
    regime["market_volatility_20"] = spy_returns.rolling(20).std()
    regime["growth_relative_20"] = (
        equity["qqq"].pct_change(20, fill_method=None) - equity["spy"].pct_change(20, fill_method=None)
    )
    regime["small_relative_20"] = (
        equity["iwm"].pct_change(20, fill_method=None) - equity["spy"].pct_change(20, fill_method=None)
    )

    regime = regime.join(vix.rename("vix_level"), how="outer")
    regime["vix_change_5"] = vix.pct_change(5, fill_method=None)
    regime = regime.join(rates.rename("rate_level"), how="outer")
    regime["rate_change_20"] = rates.diff(20)

    return regime.sort_index().replace([np.inf, -np.inf], np.nan)


def _regime_as_of(cutoffs: pd.DatetimeIndex, context_frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Backward match each value by availability, with a calendar-time stale limit.

    A context bar dated D is conservatively available at D+1 00:00 UTC.
    Matching before reindexing preserves Friday information for a Saturday
    crypto target, and also works for a target index containing only one date.
    """
    regime = _raw_regime(context_frames)
    result = pd.DataFrame(index=cutoffs, columns=REGIME_FEATURE_COLUMNS, dtype=float)
    for column in REGIME_FEATURE_COLUMNS:
        series = regime[column].dropna()
        available = series.index + pd.Timedelta(days=1)
        positions = available.get_indexer(cutoffs, method="ffill", tolerance=MAX_CONTEXT_AGE)
        valid = positions >= 0
        result.loc[valid, column] = series.to_numpy()[positions[valid]]
    return result


def build_regime_features(
    target_index: pd.DatetimeIndex, context_frames: dict[str, pd.DataFrame]
) -> pd.DataFrame:
    normalized_target = pd.DatetimeIndex(pd.to_datetime(target_index, utc=True)).normalize()
    return _regime_as_of(normalized_target, context_frames)


def latest_regime_snapshot(
    as_of: pd.Timestamp, context_frames: dict[str, pd.DataFrame]
) -> pd.DataFrame:
    """Latest regime values known at or before a completed target bar."""
    normalized = pd.Timestamp(as_of)
    if normalized.tzinfo is None:
        normalized = normalized.tz_localize("UTC")
    else:
        normalized = normalized.tz_convert("UTC")
    # Same cutoff as historical features for the following calendar day.
    latest = _regime_as_of(
        pd.DatetimeIndex([normalized.normalize() + pd.Timedelta(days=1)]), context_frames
    )
    if latest.isna().any().any():
        raise ValueError(f"no complete market regime snapshot is available as of {normalized}")
    latest.index = pd.DatetimeIndex([normalized.normalize()])
    latest.index.name = "feature_as_of"
    return latest


def attach_regime_features(
    target_features: pd.DataFrame, context_frames: dict[str, pd.DataFrame]
) -> pd.DataFrame:
    regime = build_regime_features(target_features.index, context_frames)
    combined = target_features.copy()
    combined[REGIME_FEATURE_COLUMNS] = regime[REGIME_FEATURE_COLUMNS]
    return combined.dropna(subset=REGIME_FEATURE_COLUMNS)
