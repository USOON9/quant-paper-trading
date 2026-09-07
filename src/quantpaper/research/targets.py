"""Explicit decision and label timing for an isolated daily price-proxy study.

These rows describe a temporally feasible research convention, not verified
executable fills or historically available Yahoo data vintages. US equities
use the previous exchange session's completed bar. BTC deliberately uses D-2,
so its feature bar is fully published before the target UTC day begins.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from ..ml.features import FEATURE_COLUMNS, _raw_features
from ..ml.regime import REGIME_FEATURE_COLUMNS, _regime_as_of
from ..sessions import equity_calendar


TARGET_CONTRACT_VERSION = "daily_timing_v3"
ALL_FEATURE_COLUMNS = FEATURE_COLUMNS + REGIME_FEATURE_COLUMNS
TIMING_COLUMNS = [
    "feature_date", "feature_available_at", "decision_at", "entry_at",
    "exit_at", "label_available_at",
]
OUTPUT_COLUMNS = ALL_FEATURE_COLUMNS + [
    "symbol", "target", "target_return", *TIMING_COLUMNS, "target_contract_version",
]
_DAY = timedelta(days=1)
_PUBLICATION_DELAY = timedelta(minutes=30)


def _utc_index(values) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(values, utc=True)).as_unit("ns")


def _empty_result() -> pd.DataFrame:
    result = pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="timestamp"))
    for column in ALL_FEATURE_COLUMNS + ["target_return"]:
        result[column] = pd.Series(index=result.index, dtype=float)
    result["target"] = pd.Series(index=result.index, dtype="int64")
    for column in TIMING_COLUMNS:
        result[column] = pd.Series(index=result.index, dtype="datetime64[ns, UTC]")
    for column in ("symbol", "target_contract_version"):
        result[column] = pd.Series(index=result.index, dtype=object)
    return result.reindex(columns=OUTPUT_COLUMNS)


def build_timing_dataset(
    frame: pd.DataFrame, symbol: str, context_frames: dict[str, pd.DataFrame]
) -> pd.DataFrame:
    """Build 19 features, an open-to-close price label, and explicit UTC timing.

    The index is the *target* session date, not the feature date. Missing exact
    feature sessions and incomplete 21-session lookbacks are omitted; an older
    available row is never substituted. Context features are matched backwards
    at ``decision_at`` using the v2 date+1-day availability convention.

    Equities: prior XNYS close+30m <= D 00:30 UTC decision < regular open.
    BTC: D-2 bar end+30m = D-1 00:30 UTC decision < D 00:00 UTC entry.
    Every label becomes available 30 minutes after its target session ends.
    """
    symbol = symbol.strip().upper()
    if not symbol or (symbol.endswith("-USD") and symbol != "BTC-USD"):
        raise ValueError("daily_timing_v3 supports US equity symbols and BTC-USD only")
    # Validate every input OHLCV row, including rows outside the exchange calendar.
    data, own = _raw_features(frame)
    data.index = _utc_index(data.index)
    own.index = data.index
    if data.empty:
        return _empty_result()

    if symbol == "BTC-USD":
        expected = pd.date_range(data.index.min(), data.index.max(), freq="D")
        target_dates = data.index
        feature_dates = target_dates - timedelta(days=2)
        feature_available = feature_dates + _DAY + _PUBLICATION_DELAY
        decisions = feature_available
        entries = target_dates
        exits = entries + _DAY
    else:
        calendar = equity_calendar(
            start=(data.index.min().date() - timedelta(days=14)).isoformat(),
            end=(data.index.max().date() + timedelta(days=14)).isoformat(),
        )
        schedule = calendar.schedule.copy()
        schedule.index = _utc_index(schedule.index)
        expected = schedule.index
        # Holiday/weekend rows cannot participate in either own features or labels.
        valid_sessions = data.index.isin(expected)
        if not valid_sessions.all():
            data, own = _raw_features(data.loc[valid_sessions])
            data.index = _utc_index(data.index)
            own.index = data.index
        if data.empty:
            return _empty_result()
        target_dates = data.index
        previous = pd.Series(expected, index=expected).shift(1)
        feature_dates = _utc_index(previous.reindex(target_dates))
        feature_available = _utc_index(schedule["close"].reindex(feature_dates)) + _PUBLICATION_DELAY
        decisions = target_dates + _PUBLICATION_DELAY
        entries = _utc_index(schedule["open"].reindex(target_dates))
        exits = _utc_index(schedule["close"].reindex(target_dates))

    # Twenty-session returns and volatilities require 21 exact session closes.
    # Computing _raw_features on a gapped frame alone would span missing dates.
    complete_window = (
        pd.Series(expected.isin(data.index), index=expected, dtype=int)
        .rolling(21, min_periods=21).sum().eq(21)
    )
    own.loc[~complete_window.reindex(own.index, fill_value=False), FEATURE_COLUMNS] = np.nan
    result = own.reindex(feature_dates)[FEATURE_COLUMNS].copy()
    result.index = target_dates

    regime = _regime_as_of(_utc_index(decisions), context_frames)
    regime.index = target_dates
    result[REGIME_FEATURE_COLUMNS] = regime[REGIME_FEATURE_COLUMNS]
    result["symbol"] = symbol
    result["target_return"] = data["close"] / data["open"] - 1.0
    if not np.isfinite(result["target_return"].to_numpy()).all():
        raise ValueError("target open-to-close return is not finite")
    result["target"] = (result["target_return"] > 0).astype(int)
    for column, values in zip(TIMING_COLUMNS, (
        feature_dates, feature_available, decisions, entries, exits, exits + _PUBLICATION_DELAY,
    )):
        result[column] = pd.Series(_utc_index(values), index=target_dates)
    result["target_contract_version"] = TARGET_CONTRACT_VERSION
    result.index.name = "timestamp"

    complete = np.isfinite(result[ALL_FEATURE_COLUMNS].to_numpy()).all(axis=1)
    complete &= result[TIMING_COLUMNS].notna().all(axis=1).to_numpy()
    result = result.loc[complete].reindex(columns=OUTPUT_COLUMNS).sort_index()
    chronology = (
        (result["feature_date"] < result["feature_available_at"])
        & (result["feature_available_at"] <= result["decision_at"])
        & (result["decision_at"] < result["entry_at"])
        & (result["entry_at"] < result["exit_at"])
        & (result["exit_at"] < result["label_available_at"])
    )
    if not chronology.all():
        raise ValueError("daily_timing_v3 produced an invalid feature/decision/label chronology")
    return result
