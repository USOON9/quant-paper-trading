from __future__ import annotations

import unittest

try:
    import numpy as np
    import pandas as pd

    from quantpaper.ml.features import FEATURE_COLUMNS, build_features
    from quantpaper.ml.regime import CONTEXT_SYMBOLS, REGIME_FEATURE_COLUMNS, build_regime_features
    from quantpaper.ml.training import chronological_split
    from quantpaper.ml.walkforward import ALL_FEATURE_COLUMNS, walk_forward_predictions
    from quantpaper.ml.yahoo import completed_daily_rows
except ImportError:
    np = None
    pd = None


@unittest.skipIf(pd is None, "ML optional dependencies are not installed")
class MLTests(unittest.TestCase):
    def make_frame(self, rows: int = 180):
        index = pd.date_range("2020-01-01", periods=rows, freq="B", tz="UTC")
        close = 100.0 + np.arange(rows) * 0.1 + np.sin(np.arange(rows) / 5.0)
        return pd.DataFrame(
            {
                "open": close - 0.05,
                "high": close + 0.3,
                "low": close - 0.3,
                "close": close,
                "volume": 1_000_000 + np.arange(rows) * 100,
            },
            index=index,
        )

    def test_features_are_shifted_one_session(self) -> None:
        frame = self.make_frame()
        features = build_features(frame, "TEST")
        date = features.index[10]
        previous_date = frame.index[frame.index.get_loc(date) - 1]
        expected = frame.loc[previous_date, "close"] / frame.iloc[frame.index.get_loc(previous_date) - 1]["close"] - 1
        self.assertAlmostEqual(features.loc[date, "return_1"], expected)
        self.assertEqual(set(FEATURE_COLUMNS).issubset(features.columns), True)

    def test_chronological_split_has_purge_gap(self) -> None:
        dataset = build_features(self.make_frame(), "TEST")
        train, test = chronological_split(dataset, purge_sessions=5)
        self.assertLess(train.index.max(), test.index.min())
        all_dates = sorted(dataset.index.unique())
        self.assertGreaterEqual(all_dates.index(test.index.min()) - all_dates.index(train.index.max()), 6)

    def test_regime_features_are_lagged(self) -> None:
        frame = self.make_frame()
        contexts = {symbol: frame.copy() for symbol in CONTEXT_SYMBOLS}
        regime = build_regime_features(frame.index, contexts).dropna()
        date = regime.index[0]
        location = frame.index.get_loc(date)
        expected = frame.iloc[location - 1]["close"] / frame.iloc[location - 2]["close"] - 1
        self.assertAlmostEqual(regime.loc[date, "market_return_1"], expected)
        self.assertEqual(set(REGIME_FEATURE_COLUMNS).issubset(regime.columns), True)

    def test_walk_forward_predictions_are_strictly_out_of_sample(self) -> None:
        rng = np.random.default_rng(17)
        dates = pd.date_range("2018-01-01", periods=180, freq="B", tz="UTC")
        rows = []
        for symbol_number, symbol in enumerate(("AAA", "BBB")):
            frame = pd.DataFrame(rng.normal(size=(len(dates), len(ALL_FEATURE_COLUMNS))))
            frame.columns = ALL_FEATURE_COLUMNS
            frame.index = dates
            frame["symbol"] = symbol
            frame["target_return"] = rng.normal(0, 0.01, len(dates))
            frame["target"] = (
                frame["target_return"] + frame["return_1"] * 0.01 > 0
            ).astype(int)
            rows.append(frame)
        dataset = pd.concat(rows).sort_index()
        predictions, folds = walk_forward_predictions(
            dataset,
            min_train_sessions=80,
            test_sessions=20,
            purge_sessions=5,
            max_folds=3,
        )
        self.assertEqual(len(folds), 3)
        self.assertGreater(len(predictions), 0)
        for fold in folds:
            train_end = pd.Timestamp(fold.train_end)
            test_start = pd.Timestamp(fold.test_start)
            self.assertLess(train_end, test_start)

    def test_current_daily_bar_is_excluded(self) -> None:
        frame = self.make_frame(3)
        now = pd.Timestamp("2020-01-03 12:00:00", tz="UTC")
        completed = completed_daily_rows(frame, now=now)
        self.assertEqual(len(completed), 2)
        self.assertNotIn(frame.index[-1], completed.index)


if __name__ == "__main__":
    unittest.main()
