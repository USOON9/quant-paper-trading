"""Expanding walk-forward evaluation with cost and stability diagnostics."""

from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, log_loss, roc_auc_score

from .features import FEATURE_COLUMNS, FEATURE_CONTRACT_VERSION
from .metrics import (
    BENCHMARK_DEFINITION, COST_DEFINITION, PORTFOLIO_DEFINITION,
    portfolio_daily_returns, return_metrics, strategy_metrics, validate_cost,
)
from .regime import REGIME_FEATURE_COLUMNS
from .training import build_model


ALL_FEATURE_COLUMNS = FEATURE_COLUMNS + REGIME_FEATURE_COLUMNS


@dataclass(frozen=True, slots=True)
class FoldMetrics:
    fold: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    train_rows: int
    test_rows: int
    roc_auc: float | None
    accuracy: float


@dataclass(frozen=True, slots=True)
class WalkForwardEvaluation:
    min_train_sessions: int
    test_sessions: int
    purge_sessions: int
    max_folds: int
    folds: int
    oos_start: str
    oos_end: str
    oos_rows: int
    roc_auc: float | None
    accuracy: float
    brier_score: float
    log_loss: float
    base_rate_log_loss: float
    net_return_pct: float
    annualized_sharpe: float
    max_drawdown_pct: float
    benchmark_return_pct: float
    active_predictions: int
    positive_auc_fold_fraction: float
    cost_stress: dict[str, dict[str, float]]
    approved_for_paper_signals: bool
    rejection_reasons: tuple[str, ...]
    fold_metrics: tuple[FoldMetrics, ...]


def walk_forward_predictions(
    dataset: pd.DataFrame,
    *,
    min_train_sessions: int = 1260,
    test_sessions: int = 252,
    purge_sessions: int = 5,
    max_folds: int = 12,
) -> tuple[pd.DataFrame, tuple[FoldMetrics, ...]]:
    if min_train_sessions <= 0 or test_sessions <= 0 or purge_sessions < 0 or max_folds <= 0:
        raise ValueError("training, test and fold counts must be positive; purge must be nonnegative")
    dates = pd.Index(sorted(dataset.index.unique()))
    required = min_train_sessions + purge_sessions + test_sessions
    if len(dates) < required:
        raise ValueError(f"at least {required} unique sessions are required")
    earliest_test = max(min_train_sessions + purge_sessions, len(dates) - max_folds * test_sessions)
    predictions: list[pd.DataFrame] = []
    folds: list[FoldMetrics] = []

    fold_number = 0
    for test_start_index in range(earliest_test, len(dates), test_sessions):
        train_end_index = test_start_index - purge_sessions
        test_dates = dates[test_start_index : test_start_index + test_sessions]
        if test_dates.empty:
            continue
        train_dates = dates[:train_end_index]
        train = dataset.loc[dataset.index.isin(train_dates)]
        test = dataset.loc[dataset.index.isin(test_dates)]
        if train.empty or test.empty or train["target"].nunique() < 2:
            raise ValueError("walk-forward training fold is empty or lacks both target classes")

        model = build_model()
        model.fit(train[ALL_FEATURE_COLUMNS], train["target"])
        probability = model.predict_proba(test[ALL_FEATURE_COLUMNS])[:, 1]
        predicted = (probability >= 0.5).astype(int)
        fold_number += 1
        output = test[["symbol", "target", "target_return"]].copy()
        output["probability"] = probability
        output["base_probability"] = float(train["target"].mean())
        output["fold"] = fold_number
        predictions.append(output)
        folds.append(
            FoldMetrics(
                fold=fold_number,
                train_start=train.index.min().date().isoformat(),
                train_end=train.index.max().date().isoformat(),
                test_start=test.index.min().date().isoformat(),
                test_end=test.index.max().date().isoformat(),
                train_rows=len(train),
                test_rows=len(test),
                roc_auc=(float(roc_auc_score(test["target"], probability))
                         if test["target"].nunique() == 2 else None),
                accuracy=float(accuracy_score(test["target"], predicted)),
            )
        )
    if not predictions:
        raise RuntimeError("walk-forward produced no valid out-of-sample folds")
    return pd.concat(predictions).sort_index(), tuple(folds)


def _strategy_metrics(predictions: pd.DataFrame, cost_bps: float) -> dict[str, float]:
    return strategy_metrics(predictions, cost_bps)


def evaluate_walk_forward(
    predictions: pd.DataFrame,
    folds: tuple[FoldMetrics, ...],
    cost_bps: float,
    *,
    min_train_sessions: int = 1260,
    test_sessions: int = 252,
    purge_sessions: int = 5,
    max_folds: int = 12,
) -> WalkForwardEvaluation:
    validate_cost(cost_bps)
    if not folds:
        raise ValueError("at least one chronological fold is required")
    probability = predictions["probability"].to_numpy()
    target = predictions["target"].to_numpy()
    predicted = (probability >= 0.5).astype(int)
    base = predictions["base_probability"].to_numpy()
    costs = sorted({0.0, float(cost_bps), float(cost_bps) * 2.0, 20.0})
    stress = {f"{cost:g}_bps": _strategy_metrics(predictions, cost) for cost in costs}
    selected = stress[f"{cost_bps:g}_bps"]
    benchmark_daily = portfolio_daily_returns(predictions, predictions["target_return"].to_numpy())
    benchmark = return_metrics(benchmark_daily)
    signals = np.where(probability >= 0.55, 1, np.where(probability <= 0.45, -1, 0))
    auc = float(roc_auc_score(target, probability)) if len(np.unique(target)) == 2 else None
    positive_fraction = float(np.mean([fold.roc_auc is not None and fold.roc_auc > 0.5 for fold in folds]))
    model_loss = float(log_loss(target, probability, labels=[0, 1]))
    baseline_loss = float(log_loss(target, base, labels=[0, 1]))
    rejection: list[str] = []
    if auc is None or auc < 0.53:
        rejection.append("walk-forward ROC AUC is below 0.53")
    if model_loss >= baseline_loss:
        rejection.append("probability log loss does not beat the training-only base rate")
    if selected["annualized_sharpe"] < 0.75:
        rejection.append("walk-forward Sharpe is below 0.75")
    if selected["max_drawdown_pct"] < -20.0:
        rejection.append("walk-forward maximum drawdown exceeds 20%")
    if positive_fraction < 0.60:
        rejection.append("fewer than 60% of folds have ROC AUC above 0.50")
    doubled = stress[f"{cost_bps * 2:g}_bps"]
    if doubled["net_return_pct"] <= 0.0:
        rejection.append("return is not positive at twice the assumed trading cost")

    return WalkForwardEvaluation(
        min_train_sessions=min_train_sessions,
        test_sessions=test_sessions,
        purge_sessions=purge_sessions,
        max_folds=max_folds,
        folds=len(folds),
        oos_start=predictions.index.min().date().isoformat(),
        oos_end=predictions.index.max().date().isoformat(),
        oos_rows=len(predictions),
        roc_auc=auc,
        accuracy=float(accuracy_score(target, predicted)),
        brier_score=float(np.mean((probability - target) ** 2)),
        log_loss=model_loss,
        base_rate_log_loss=baseline_loss,
        net_return_pct=selected["net_return_pct"],
        annualized_sharpe=selected["annualized_sharpe"],
        max_drawdown_pct=selected["max_drawdown_pct"],
        benchmark_return_pct=benchmark["net_return_pct"],
        active_predictions=int(np.count_nonzero(signals)),
        positive_auc_fold_fraction=positive_fraction,
        cost_stress=stress,
        approved_for_paper_signals=not rejection,
        rejection_reasons=tuple(rejection),
        fold_metrics=folds,
    )


def train_walk_forward(
    dataset: pd.DataFrame,
    model_path: Path,
    metadata_path: Path,
    predictions_path: Path,
    fingerprint: str,
    cost_bps: float = 5.0,
) -> WalkForwardEvaluation:
    validate_cost(cost_bps)
    predictions, folds = walk_forward_predictions(dataset)
    evaluation = evaluate_walk_forward(predictions, folds, cost_bps)
    final_model = build_model()
    final_model.fit(dataset[ALL_FEATURE_COLUMNS], dataset["target"])
    model_path.parent.mkdir(parents=True, exist_ok=True)
    predictions_path.parent.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc).isoformat()
    joblib.dump(
        {
            "model": final_model,
            "features": ALL_FEATURE_COLUMNS,
            "symbols": sorted(dataset["symbol"].unique()),
            "trained_through": dataset.index.max().date().isoformat(),
            "dataset_fingerprint": fingerprint,
            "evaluation_method": "expanding_walk_forward_with_purge",
            "feature_contract_version": FEATURE_CONTRACT_VERSION,
            "created_at": created_at,
        },
        model_path,
    )
    predictions.to_csv(predictions_path, index_label="timestamp")
    metadata = {
        "evaluation": asdict(evaluation),
        "dataset_fingerprint": fingerprint,
        "feature_contract_version": FEATURE_CONTRACT_VERSION,
        "created_at": created_at,
        "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "portfolio_definition": PORTFOLIO_DEFINITION,
        "cost_definition": COST_DEFINITION,
        "benchmark_definition": BENCHMARK_DEFINITION,
        "cost_bps": cost_bps,
        "random_seed": 17,
        "early_stopping": False,
        "annualization_calendar_days": 365.25,
        "libraries": {
            package: version(package)
            for package in ("numpy", "pandas", "scikit-learn", "yfinance", "joblib")
        },
    }
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return evaluation
