"""Separated, long/flat research diagnostics; no broker or promotion interface."""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from ..ml.metrics import portfolio_daily_returns, return_metrics, validate_cost
from ..ml.training import build_model
from ..ml.walkforward import ALL_FEATURE_COLUMNS


TIMES = ("feature_available_at", "decision_at", "entry_at", "exit_at", "label_available_at")


def validate_dataset(dataset: pd.DataFrame, symbols: list[str]) -> None:
    if dataset.empty:
        raise ValueError("no usable target rows after exact-date alignment and warmup")
    if dataset.index.tz is None or dataset.index.hasnans:
        raise ValueError("target index must be timezone-aware and nonmissing")
    if set(dataset["symbol"]) != set(symbols):
        raise ValueError("each declared symbol must have usable data; cannot silently change universe")
    keys = pd.DataFrame({"date": dataset.index, "symbol": dataset["symbol"].to_numpy()})
    if keys.duplicated().any():
        raise ValueError("duplicate symbol/target session")
    if not (dataset["target_contract_version"] == "daily_timing_v3").all():
        raise ValueError("research targets must use daily_timing_v3")
    values = dataset[ALL_FEATURE_COLUMNS + ["target_return", "target"]].to_numpy(dtype=float)
    if not np.isfinite(values).all() or not dataset["target"].isin([0, 1]).all():
        raise ValueError("finite features/labels and binary directions are required")
    if not (dataset["target"] == (dataset["target_return"] > 0).astype(int)).all():
        raise ValueError("direction label disagrees with return")
    for name in TIMES:
        if not isinstance(dataset[name].dtype, pd.DatetimeTZDtype) or dataset[name].isna().any():
            raise ValueError(f"{name} must contain timezone-aware timestamps")
    valid = (
        (dataset["feature_available_at"] <= dataset["decision_at"])
        & (dataset["decision_at"] < dataset["entry_at"])
        & (dataset["entry_at"] < dataset["exit_at"])
        & (dataset["exit_at"] < dataset["label_available_at"])
    )
    if not valid.all():
        raise ValueError("feature/decision/entry/exit/label chronology violated")


def chronological_folds(dataset: pd.DataFrame, validation: dict):
    """Purge sessions AND unavailable labels, including equality at decision."""
    dates = pd.Index(sorted(dataset.index.unique()))
    minimum = validation["min_train_sessions"]
    test_size = validation["test_sessions"]
    gap = validation["purge_sessions"]
    count = validation["max_folds"]
    if min(minimum, test_size, count) <= 0 or gap < 0:
        raise ValueError("invalid fold sizes")
    if len(dates) < minimum + gap + test_size:
        raise ValueError("insufficient historical sessions for frozen validation protocol")
    start = max(minimum + gap, len(dates) - count * test_size)
    for fold_id, offset in enumerate(range(start, len(dates), test_size), 1):
        test = dataset.loc[dataset.index.isin(dates[offset:offset + test_size])].copy()
        train = dataset.loc[dataset.index.isin(dates[:offset - gap])].copy()
        first_decision = test["decision_at"].min()
        train = train.loc[train["label_available_at"] < first_decision]
        if train.index.nunique() < minimum or train["target"].nunique() != 2:
            raise ValueError("fold lacks sufficient available training labels or both classes")
        yield fold_id, train, test


def _estimator(name: str):
    if name == "hist_gradient_boosting":
        return build_model()
    if name == "logistic":
        return make_pipeline(
            SimpleImputer(strategy="median"), StandardScaler(),
            LogisticRegression(C=1.0, max_iter=1000, random_state=17),
        )
    raise ValueError(f"unknown frozen estimator: {name}")


def long_flat_daily(predictions: pd.DataFrame, cost_bps: float, threshold: float) -> pd.Series:
    validate_cost(cost_bps)
    probability = predictions["probability"].to_numpy(dtype=float)
    if not np.isfinite(probability).all() or ((probability < 0) | (probability > 1)).any():
        raise ValueError("invalid probabilities")
    active = probability >= threshold
    row_returns = active * (predictions["target_return"].to_numpy() - cost_bps / 10_000)
    return portfolio_daily_returns(predictions, row_returns)


def _mean_return_interval(daily: pd.Series) -> dict:
    """Descriptive moving-block interval, not selection-adjusted significance."""
    values = daily.to_numpy()
    block = min(20, len(values))
    rng = np.random.default_rng(17)
    starts = rng.integers(0, len(values), size=(500, (len(values) + block - 1) // block))
    indices = (starts[..., None] + np.arange(block)) % len(values)
    means = values[indices.reshape(500, -1)[:, :len(values)]].mean(axis=1) * 10_000
    low, high = np.quantile(means, [0.025, 0.975])
    return {
        "mean_daily_bps": float(values.mean() * 10_000),
        "lower_95_bps": float(low), "upper_95_bps": float(high),
        "block_calendar_days": block, "resamples": 500,
        "interpretation": "descriptive circular-block bootstrap; not adjusted for model selection",
    }


def _classification(frame: pd.DataFrame) -> dict:
    target = frame["target"].to_numpy()
    probability = frame["probability"].to_numpy()
    return {
        "roc_auc": float(roc_auc_score(target, probability)) if len(np.unique(target)) == 2 else None,
        "accuracy": float(np.mean((probability >= 0.5) == target)),
        "log_loss": float(log_loss(target, probability, labels=[0, 1])),
        "brier_score": float(np.mean((probability - target) ** 2)),
    }


def evaluate_group(dataset: pd.DataFrame, group_config: dict, protocol: dict) -> tuple[dict, pd.DataFrame]:
    validate_dataset(dataset, group_config["symbols"])
    outputs = []
    folds = []
    for fold_id, train, test in chronological_folds(dataset, protocol["validation"]):
        folds.append({
            "fold": fold_id,
            "train_start": train.index.min().date().isoformat(),
            "train_end": train.index.max().date().isoformat(),
            "test_start": test.index.min().date().isoformat(),
            "test_end": test.index.max().date().isoformat(),
            "train_rows": len(train), "test_rows": len(test),
            "last_train_label_available_at": train["label_available_at"].max().isoformat(),
            "first_test_decision_at": test["decision_at"].min().isoformat(),
        })
        base_probability = float(train["target"].mean())
        for model_name in protocol["models"]:
            if model_name == "base_rate":
                probabilities = np.full(len(test), base_probability)
            else:
                model = _estimator(model_name)
                model.fit(train[ALL_FEATURE_COLUMNS], train["target"])
                probabilities = model.predict_proba(test[ALL_FEATURE_COLUMNS])[:, 1]
            out = test.drop(columns=ALL_FEATURE_COLUMNS).copy()
            out["probability"] = probabilities
            out["base_probability"] = base_probability
            out["model"] = model_name
            out["fold"] = fold_id
            outputs.append(out)
    predictions = pd.concat(outputs).sort_index()
    threshold = protocol["long_threshold"]
    selected_cost = group_config["selected_cost_bps"]
    models = {}
    for name in protocol["models"]:
        subset = predictions.loc[predictions["model"] == name]
        selected_daily = long_flat_daily(subset, selected_cost, threshold)
        metrics = {
            **_classification(subset),
            "active_predictions": int((subset["probability"] >= threshold).sum()),
            "base_rate_log_loss": float(log_loss(subset["target"], subset["base_probability"], labels=[0, 1])),
            "cost_stress": {
                f"{cost:g}_bps": return_metrics(long_flat_daily(subset, cost, threshold))
                for cost in group_config["costs_bps"]
            },
            "mean_return_interval": _mean_return_interval(selected_daily),
            "per_symbol": {},
        }
        for symbol in group_config["symbols"]:
            symbol_rows = subset.loc[subset["symbol"] == symbol]
            metrics["per_symbol"][symbol] = {
                **_classification(symbol_rows),
                **return_metrics(long_flat_daily(symbol_rows, selected_cost, threshold)),
                "observations": len(symbol_rows),
            }
        models[name] = metrics
    reference = predictions.loc[predictions["model"] == "base_rate"]
    benchmark = reference.assign(probability=1.0)
    return {
        "symbols": group_config["symbols"], "position_policy": "long_flat",
        "selected_cost_bps": selected_cost,
        "oos_start": reference.index.min().date().isoformat(),
        "oos_end": reference.index.max().date().isoformat(), "oos_rows": len(reference),
        "folds": folds, "models": models,
        "always_long_intraday_benchmark": return_metrics(long_flat_daily(benchmark, selected_cost, threshold)),
        "benchmark_definition": "daily open-to-close long, same round-trip cost, flat overnight; NOT buy-and-hold",
        "portfolio_definition": "fixed equal symbol capital weights, cash on missing/flat, calendar-day returns",
        "selection": "no automatic winner or promotion; all frozen candidates reported",
    }, predictions
