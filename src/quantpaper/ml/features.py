"""Point-in-time daily features and next-session labels.

Features on trading date T are shifted by one session, so they contain only
information known after T-1 close. The target is T open-to-close return.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


FEATURE_CONTRACT_VERSION = "daily_pit_v2"


FEATURE_COLUMNS = [
    "return_1",
    "return_5",
    "return_20",
    "volatility_5",
    "volatility_20",
    "ma_gap_5",
    "ma_gap_20",
    "range_pct",
    "volume_z20",
    "rsi_14",
]


def _raw_features(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    data = frame.copy().sort_index()
    data.index = pd.to_datetime(data.index, utc=True).normalize()
    if data.index.hasnans or data.index.has_duplicates:
        raise ValueError("daily bars must have unique, valid session dates")
    required = ["open", "high", "low", "close", "volume"]
    values = data[required].astype(float)
    if not np.isfinite(values.to_numpy()).all():
        raise ValueError("daily bars contain missing or non-finite OHLCV values")
    if (values[["open", "high", "low", "close"]] <= 0).any().any() or (values["volume"] < 0).any():
        raise ValueError("daily bars require positive prices and nonnegative volume")
    # Adjusted Yahoo OHLC can differ by a few floating point ULPs at the bounds.
    tolerance = values[["open", "high", "low", "close"]].max(axis=1) * 1e-12
    if ((values["high"] + tolerance < values[["open", "close", "low"]].max(axis=1)) |
        (values["low"] - tolerance > values[["open", "close", "high"]].min(axis=1))).any():
        raise ValueError("daily bars have inconsistent OHLC bounds")
    data[required] = values
    close = data["close"].astype(float)
    returns = close.pct_change(fill_method=None)
    volume = data["volume"].astype(float)

    features = pd.DataFrame(index=data.index)
    features["return_1"] = returns
    features["return_5"] = close.pct_change(5, fill_method=None)
    features["return_20"] = close.pct_change(20, fill_method=None)
    features["volatility_5"] = returns.rolling(5).std()
    features["volatility_20"] = returns.rolling(20).std()
    features["ma_gap_5"] = close / close.rolling(5).mean() - 1.0
    features["ma_gap_20"] = close / close.rolling(20).mean() - 1.0
    features["range_pct"] = (data["high"] - data["low"]) / close
    volume_std = volume.rolling(20).std().replace(0.0, np.nan)
    features["volume_z20"] = (volume - volume.rolling(20).mean()) / volume_std

    difference = close.diff()
    average_gain = difference.clip(lower=0).rolling(14).mean()
    average_loss = -difference.clip(upper=0).rolling(14).mean()
    relative_strength = average_gain / average_loss.replace(0.0, np.nan)
    features["rsi_14"] = 100.0 - 100.0 / (1.0 + relative_strength)
    features.loc[(average_loss == 0) & (average_gain > 0), "rsi_14"] = 100.0
    features.loc[(average_loss == 0) & (average_gain == 0), "rsi_14"] = 50.0

    return data, features.replace([np.inf, -np.inf], np.nan)


def build_features(frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    data, features = _raw_features(frame)
    # The shift is the central anti-leakage guarantee for target session T.
    features[FEATURE_COLUMNS] = features[FEATURE_COLUMNS].shift(1)
    features["target_return"] = data["close"].astype(float) / data["open"].astype(float) - 1.0
    features["target"] = (features["target_return"] > 0.0).astype(int)
    features["symbol"] = symbol
    features.index.name = "timestamp"
    return features.replace([np.inf, -np.inf], np.nan).dropna()


def latest_feature_snapshot(frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Features known after the latest completed bar, for a future session."""
    _, features = _raw_features(frame)
    latest = features.tail(1).copy()
    if latest.empty or latest[FEATURE_COLUMNS].isna().any().any():
        raise ValueError(f"not enough completed history to build features for {symbol}")
    latest["symbol"] = symbol
    latest.index.name = "feature_as_of"
    return latest
