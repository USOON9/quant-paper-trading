from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

try:
    import joblib
    import numpy as np
    import pandas as pd

    from quantpaper.ml.features import FEATURE_COLUMNS, FEATURE_CONTRACT_VERSION, build_features, latest_feature_snapshot
    from quantpaper.ml.metrics import portfolio_daily_returns, return_metrics, strategy_metrics
    from quantpaper.ml.regime import CONTEXT_SYMBOLS, build_regime_features, latest_regime_snapshot
    from quantpaper.ml.training import build_model, chronological_split, train_and_evaluate
    from quantpaper.ml.walkforward import ALL_FEATURE_COLUMNS, evaluate_walk_forward, walk_forward_predictions
except ImportError:
    pd = None


@unittest.skipIf(pd is None, "ML optional dependencies are not installed")
class MLReviewTests(unittest.TestCase):
    def frame(self) -> pd.DataFrame:
        dates = pd.bdate_range("2026-01-01", "2026-03-06", tz="UTC")
        prices = 100 + np.arange(len(dates), dtype=float)
        return pd.DataFrame({
            "open": prices - 0.1, "high": prices + 0.2, "low": prices - 0.3,
            "close": prices, "volume": 1000 + np.arange(len(dates)),
        }, index=dates)

    def test_weekend_and_sparse_regime_match_inference(self) -> None:
        frame = self.frame()
        context = {symbol: frame.copy() for symbol in CONTEXT_SYMBOLS}
        # March 6 is Friday. Saturday must use Friday, not Thursday.
        target = pd.DatetimeIndex([pd.Timestamp("2026-03-07", tz="UTC")])
        historical = build_regime_features(target, context)
        expected = frame.iloc[-1]["close"] / frame.iloc[-2]["close"] - 1
        self.assertAlmostEqual(historical.iloc[0]["market_return_1"], expected)
        live = latest_regime_snapshot(target[0] - pd.Timedelta(days=1), context)
        np.testing.assert_allclose(historical.iloc[0], live.iloc[0])
        monday = build_regime_features(pd.DatetimeIndex([target[0] + pd.Timedelta(days=2)]), context)
        np.testing.assert_allclose(monday.iloc[0], historical.iloc[0])

    def test_future_context_does_not_change_historical_features(self) -> None:
        frame = self.frame()
        target = pd.DatetimeIndex([frame.index[-2]])
        context = {symbol: frame.copy() for symbol in CONTEXT_SYMBOLS}
        before = build_regime_features(target, context)
        for current in context.values():
            current.loc[current.index >= target[0], "close"] *= 10
        after = build_regime_features(target, context)
        pd.testing.assert_frame_equal(before, after)

    def test_context_staleness_is_calendar_time_not_target_row_count(self) -> None:
        context = {symbol: self.frame() for symbol in CONTEXT_SYMBOLS}
        with self.assertRaisesRegex(ValueError, "no complete market regime"):
            latest_regime_snapshot(pd.Timestamp("2026-03-20", tz="UTC"), context)

    def test_rsi_accepts_a_monotonic_rally(self) -> None:
        frame = self.frame()
        live = latest_feature_snapshot(frame, "SPY")
        self.assertEqual(live.iloc[0]["rsi_14"], 100.0)
        historical = build_features(frame, "SPY")
        self.assertGreater(len(historical), 0)

    def test_invalid_latest_features_do_not_fall_back_to_an_old_bar(self) -> None:
        frame = self.frame()
        frame.loc[frame.index[-20:], "volume"] = 1000
        with self.assertRaisesRegex(ValueError, "not enough completed history"):
            latest_feature_snapshot(frame, "SPY")

    def test_conflicting_daily_dates_and_invalid_prices_are_rejected(self) -> None:
        frame = self.frame()
        with self.assertRaisesRegex(ValueError, "unique"):
            build_features(pd.concat([frame, frame.tail(1)]), "SPY")
        frame.loc[frame.index[-1], "high"] = np.nextafter(frame.iloc[-1]["close"], -np.inf)
        self.assertFalse(build_features(frame, "SPY").empty)
        frame.loc[frame.index[-1], "high"] = 1
        with self.assertRaisesRegex(ValueError, "OHLC bounds"):
            build_features(frame, "SPY")

    def test_weekends_keep_equity_capital_in_cash(self) -> None:
        predictions = pd.DataFrame({
            "symbol": ["SPY", "BTC-USD", "BTC-USD", "BTC-USD"],
        }, index=pd.to_datetime(["2026-03-06", "2026-03-06", "2026-03-07", "2026-03-09"], utc=True))
        daily = portfolio_daily_returns(predictions, np.array([0.01, 0.01, 0.01, 0.0]))
        np.testing.assert_allclose(daily.to_numpy(), [0.01, 0.005, 0.0, 0.0])

    def test_first_loss_and_round_trip_cost_are_counted(self) -> None:
        predictions = pd.DataFrame({"symbol": ["SPY"], "probability": [0.6], "target_return": [-0.01]},
                                   index=pd.to_datetime(["2026-03-06"], utc=True))
        metrics = strategy_metrics(predictions, 10)
        self.assertAlmostEqual(metrics["max_drawdown_pct"], -1.1)
        self.assertAlmostEqual(metrics["net_return_pct"], -1.1)
        predictions["probability"] = 0.5
        self.assertEqual(strategy_metrics(predictions, 10)["net_return_pct"], 0.0)
        for cost in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                strategy_metrics(predictions, cost)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            portfolio_daily_returns(pd.concat([predictions, predictions]), np.zeros(2))

    def test_calendar_annualization_matches_explicit_formula(self) -> None:
        daily = pd.Series([0.01, -0.005, 0, 0])
        expected = daily.mean() / daily.std(ddof=1) * np.sqrt(365.25)
        self.assertAlmostEqual(return_metrics(daily)["annualized_sharpe"], expected)

    def test_single_class_test_fold_is_retained(self) -> None:
        rng = np.random.default_rng(17)
        dataset = pd.DataFrame(rng.normal(size=(40, len(ALL_FEATURE_COLUMNS))), columns=ALL_FEATURE_COLUMNS,
                               index=pd.date_range("2020-01-01", periods=40, tz="UTC"))
        dataset["symbol"] = "SPY"
        dataset["target"] = [0, 1] * 10 + [1] * 20
        dataset["target_return"] = np.where(dataset["target"] == 1, 0.01, -0.01)
        predictions, folds = walk_forward_predictions(dataset, min_train_sessions=20, test_sessions=10,
                                                       purge_sessions=1, max_folds=2)
        self.assertEqual(len(predictions), 19)
        self.assertEqual(len(folds), 2)
        self.assertTrue(all(fold.roc_auc is None for fold in folds))
        evaluation = evaluate_walk_forward(predictions, folds, 5)
        self.assertIsNone(evaluation.roc_auc)
        self.assertFalse(evaluation.approved_for_paper_signals)

    def test_model_metadata_binds_artifact_to_feature_contract(self) -> None:
        rng = np.random.default_rng(17)
        dataset = pd.DataFrame(rng.normal(size=(140, len(FEATURE_COLUMNS))), columns=FEATURE_COLUMNS,
                               index=pd.date_range("2020-01-01", periods=140, tz="UTC"))
        dataset["symbol"] = "SPY"
        dataset["target"] = [0, 1] * 70
        dataset["target_return"] = np.where(dataset["target"] == 1, 0.01, -0.01)
        self.assertFalse(build_model().named_steps["classifier"].early_stopping)
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) / "model.joblib"
            metadata_path = Path(directory) / "nested" / "model.json"
            train_and_evaluate(dataset, model_path, metadata_path, "test-only")
            artifact = joblib.load(model_path)
            metadata = json.loads(metadata_path.read_text())
            self.assertEqual(artifact["feature_contract_version"], FEATURE_CONTRACT_VERSION)
            self.assertEqual(metadata["feature_contract_version"], FEATURE_CONTRACT_VERSION)
            self.assertEqual(metadata["model_sha256"], hashlib.sha256(model_path.read_bytes()).hexdigest())

    def test_invalid_temporal_split_cannot_leak_or_empty_training(self) -> None:
        dataset = pd.DataFrame(index=pd.date_range("2020-01-01", periods=140, tz="UTC"))
        for fraction, purge in ((0, 5), (1, 5), (0.2, -1), (0.9, 30)):
            with self.assertRaises(ValueError):
                chronological_split(dataset, test_fraction=fraction, purge_sessions=purge)


if __name__ == "__main__":
    unittest.main()
