from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from quantpaper.ml.walkforward import ALL_FEATURE_COLUMNS
from quantpaper.research.evaluation import (
    chronological_folds, evaluate_group, long_flat_daily, validate_dataset,
)


def sample_dataset(size=80):
    dates = pd.date_range("2020-01-01", periods=size, tz="UTC")
    rng = np.random.default_rng(3)
    data = pd.DataFrame(rng.normal(size=(size, 19)), index=dates, columns=ALL_FEATURE_COLUMNS)
    data["target_return"] = np.where(np.arange(size) % 2, 0.01, -0.01)
    data["target"] = (data["target_return"] > 0).astype(int)
    data["symbol"] = "BTC-USD"
    data["feature_date"] = dates - pd.Timedelta(days=2)
    data["feature_available_at"] = dates - pd.Timedelta(days=1) + pd.Timedelta(minutes=30)
    data["decision_at"] = data["feature_available_at"]
    data["entry_at"] = dates
    data["exit_at"] = dates + pd.Timedelta(days=1)
    data["label_available_at"] = data["exit_at"] + pd.Timedelta(minutes=30)
    data["target_contract_version"] = "daily_timing_v3"
    return data


VALIDATION = {"min_train_sessions": 20, "test_sessions": 15, "purge_sessions": 5, "max_folds": 2}


class ResearchEvaluationTests(unittest.TestCase):
    def test_long_flat_never_creates_crypto_short_and_charges_only_active(self):
        data = sample_dataset(2).assign(probability=[0.1, 0.9])
        returns = long_flat_daily(data, 5, 0.55)
        self.assertEqual(returns.iloc[0], 0)
        self.assertAlmostEqual(returns.iloc[1], 0.0095)

    def test_explicit_label_purge_excludes_equal_or_future_available_labels(self):
        data = sample_dataset()
        decision = data.iloc[50]["decision_at"]
        data.loc[data.index[10], "label_available_at"] = decision
        data.loc[data.index[11], "label_available_at"] = decision + pd.Timedelta(days=1)
        _, train, test = next(chronological_folds(data, VALIDATION))
        self.assertNotIn(data.index[10], train.index)
        self.assertNotIn(data.index[11], train.index)
        self.assertLess(train["label_available_at"].max(), test["decision_at"].min())

    def test_invalid_decision_or_nonfinite_features_fail(self):
        data = sample_dataset()
        data.loc[data.index[0], "decision_at"] = data.iloc[0]["entry_at"]
        with self.assertRaisesRegex(ValueError, "chronology"):
            validate_dataset(data, ["BTC-USD"])
        data = sample_dataset()
        data.loc[data.index[0], ALL_FEATURE_COLUMNS[0]] = np.inf
        with self.assertRaisesRegex(ValueError, "finite"):
            validate_dataset(data, ["BTC-USD"])

    def test_duplicate_or_missing_declared_symbol_fails(self):
        data = sample_dataset()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_dataset(pd.concat([data, data.iloc[:1]]), ["BTC-USD"])
        with self.assertRaisesRegex(ValueError, "declared"):
            validate_dataset(data, ["BTC-USD", "SPY"])

    def test_all_candidates_costs_and_label_purge_are_reported(self):
        protocol = {"validation": VALIDATION, "long_threshold": 0.55,
                    "models": ["base_rate", "logistic", "hist_gradient_boosting"]}
        group = {"symbols": ["BTC-USD"], "costs_bps": [0, 10, 25, 50], "selected_cost_bps": 25}
        report, predictions = evaluate_group(sample_dataset(), group, protocol)
        self.assertEqual(set(report["models"]), set(protocol["models"]))
        self.assertEqual(len(predictions), 30 * 3)
        for fold in report["folds"]:
            self.assertLess(fold["last_train_label_available_at"], fold["first_test_decision_at"])
        for metrics in report["models"].values():
            self.assertEqual(set(metrics["cost_stress"]), {"0_bps", "10_bps", "25_bps", "50_bps"})
            self.assertGreaterEqual(metrics["cost_stress"]["0_bps"]["net_return_pct"],
                                    metrics["cost_stress"]["50_bps"]["net_return_pct"])
            self.assertIn("BTC-USD", metrics["per_symbol"])


if __name__ == "__main__":
    unittest.main()
