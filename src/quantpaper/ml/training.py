"""Chronological training, purged evaluation and artifact persistence."""

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
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import accuracy_score, log_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.impute import SimpleImputer

from .features import FEATURE_COLUMNS, FEATURE_CONTRACT_VERSION
from .metrics import BENCHMARK_DEFINITION, COST_DEFINITION, PORTFOLIO_DEFINITION, strategy_metrics, validate_cost


def build_model() -> Pipeline:
    """Return the frozen baseline estimator used by every chronological fold."""
    return Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
            (
                "classifier",
                HistGradientBoostingClassifier(
                    learning_rate=0.05,
                    max_iter=250,
                    max_leaf_nodes=15,
                    min_samples_leaf=30,
                    l2_regularization=1.0,
                    random_state=17,
                    # The default 'auto' randomly withholds 10% above 10k rows.
                    # Use the frozen iteration count; all validation is temporal.
                    early_stopping=False,
                ),
            ),
        ]
    )


@dataclass(frozen=True, slots=True)
class Evaluation:
    train_rows: int
    test_rows: int
    train_end: str
    test_start: str
    accuracy: float
    roc_auc: float | None
    log_loss: float
    net_return_pct: float
    annualized_sharpe: float
    max_drawdown_pct: float
    active_predictions: int
    approved_for_paper: bool
    rejection_reasons: tuple[str, ...]


def chronological_split(
    dataset: pd.DataFrame, test_fraction: float = 0.20, purge_sessions: int = 5
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not 0.0 < test_fraction < 1.0 or purge_sessions < 0:
        raise ValueError("test_fraction must lie in (0, 1) and purge_sessions must be nonnegative")
    dates = pd.Index(sorted(dataset.index.unique()))
    if len(dates) < 100:
        raise ValueError("at least 100 unique sessions are required")
    split_index = int(len(dates) * (1.0 - test_fraction))
    if split_index <= purge_sessions or split_index >= len(dates):
        raise ValueError("split leaves no training or test sessions after purging")
    train_dates = dates[: split_index - purge_sessions]
    test_dates = dates[split_index:]
    return dataset.loc[dataset.index.isin(train_dates)], dataset.loc[dataset.index.isin(test_dates)]


def train_and_evaluate(
    dataset: pd.DataFrame,
    model_path: Path,
    metadata_path: Path,
    fingerprint: str,
    cost_bps: float = 5.0,
) -> Evaluation:
    validate_cost(cost_bps)
    train, test = chronological_split(dataset)
    x_train, y_train = train[FEATURE_COLUMNS], train["target"]
    x_test, y_test = test[FEATURE_COLUMNS], test["target"]
    if y_train.nunique() != 2:
        raise ValueError("training requires both target classes")

    model = build_model()
    model.fit(x_train, y_train)
    probabilities = model.predict_proba(x_test)[:, 1]
    predictions = (probabilities >= 0.5).astype(int)
    signals = np.where(probabilities >= 0.55, 1.0, np.where(probabilities <= 0.45, -1.0, 0.0))
    financial = strategy_metrics(test.assign(probability=probabilities), cost_bps)
    sharpe = financial["annualized_sharpe"]
    accuracy = float(accuracy_score(y_test, predictions))
    roc_auc = float(roc_auc_score(y_test, probabilities)) if y_test.nunique() == 2 else None
    net_return_pct = financial["net_return_pct"]
    max_drawdown_pct = financial["max_drawdown_pct"]
    rejection_reasons: list[str] = []
    if roc_auc is None or roc_auc < 0.53:
        rejection_reasons.append("ROC AUC is below 0.53")
    if sharpe < 0.75:
        rejection_reasons.append("annualized Sharpe is below 0.75")
    if max_drawdown_pct < -20.0:
        rejection_reasons.append("maximum drawdown exceeds 20%")
    if net_return_pct <= 0.0:
        rejection_reasons.append("net out-of-sample return is not positive")

    evaluation = Evaluation(
        train_rows=len(train),
        test_rows=len(test),
        train_end=train.index.max().date().isoformat(),
        test_start=test.index.min().date().isoformat(),
        accuracy=accuracy,
        roc_auc=roc_auc,
        log_loss=float(log_loss(y_test, probabilities, labels=[0, 1])),
        net_return_pct=net_return_pct,
        annualized_sharpe=sharpe,
        max_drawdown_pct=max_drawdown_pct,
        active_predictions=int(np.count_nonzero(signals)),
        approved_for_paper=not rejection_reasons,
        rejection_reasons=tuple(rejection_reasons),
    )

    model_path.parent.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc).isoformat()
    joblib.dump(
        {
            "model": model,
            "features": FEATURE_COLUMNS,
            "symbols": sorted(dataset["symbol"].unique().tolist()),
            "trained_through": evaluation.train_end,
            "dataset_fingerprint": fingerprint,
            "feature_contract_version": FEATURE_CONTRACT_VERSION,
            "created_at": created_at,
        },
        model_path,
    )
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
