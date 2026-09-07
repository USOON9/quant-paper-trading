"""Timing-contract regressions independent of any fitted model or broker."""

import unittest

import numpy as np
import pandas as pd

from quantpaper.ml.features import FEATURE_COLUMNS, _raw_features
from quantpaper.ml.regime import CONTEXT_SYMBOLS
from quantpaper.research.targets import (
    ALL_FEATURE_COLUMNS, OUTPUT_COLUMNS, TARGET_CONTRACT_VERSION,
    TIMING_COLUMNS, build_timing_dataset,
)
from quantpaper.sessions import equity_calendar


class ResearchTargetTests(unittest.TestCase):
    def frame(self, start="2026-01-01", end="2026-12-04", crypto=False):
        if crypto:
            dates = pd.date_range(start, end, tz="UTC")
        else:
            calendar = equity_calendar(start=start, end=end)
            dates = pd.DatetimeIndex(pd.to_datetime(calendar.sessions, utc=True))
        positions = np.arange(len(dates), dtype=float)
        close = 100 + positions * 0.1 + np.sin(positions / 3)
        opening = close * (1 + 0.002 * np.cos(positions))
        return pd.DataFrame({
            "open": opening, "high": np.maximum(opening, close) + 1,
            "low": np.minimum(opening, close) - 1, "close": close,
            "volume": 1000 + positions % 7 * 100,
        }, index=dates)

    def context(self, start="2026-01-01", end="2026-12-04"):
        return {symbol: self.frame(start, end) for symbol in CONTEXT_SYMBOLS}

    def test_equity_holiday_uses_exact_prior_exchange_session(self):
        dataset = build_timing_dataset(self.frame(), "SPY", self.context())
        row = dataset.loc["2026-09-08"]
        self.assertEqual(row["feature_date"], pd.Timestamp("2026-09-04T00:00:00Z"))
        self.assertEqual(row["feature_available_at"], pd.Timestamp("2026-09-04T20:30:00Z"))
        self.assertEqual(row["decision_at"], pd.Timestamp("2026-09-08T00:30:00Z"))
        self.assertEqual(row["entry_at"], pd.Timestamp("2026-09-08T13:30:00Z"))
        self.assertEqual(row["exit_at"], pd.Timestamp("2026-09-08T20:00:00Z"))
        self.assertNotIn(pd.Timestamp("2026-09-07", tz="UTC"), dataset.index)

    def test_early_close_applies_to_features_and_labels(self):
        dataset = build_timing_dataset(self.frame(), "SPY", self.context())
        self.assertEqual(dataset.loc["2026-11-27", "exit_at"], pd.Timestamp("2026-11-27T18:00:00Z"))
        self.assertEqual(dataset.loc["2026-11-27", "label_available_at"], pd.Timestamp("2026-11-27T18:30:00Z"))
        self.assertEqual(dataset.loc["2026-11-30", "feature_available_at"], pd.Timestamp("2026-11-27T18:30:00Z"))

    def test_both_dst_transitions_follow_exchange_local_hours(self):
        dataset = build_timing_dataset(self.frame(), "SPY", self.context())
        for day, utc_open in (("2026-03-06", "14:30"), ("2026-03-09", "13:30"),
                              ("2026-10-30", "13:30"), ("2026-11-02", "14:30")):
            self.assertEqual(dataset.loc[day, "entry_at"], pd.Timestamp(f"{day}T{utc_open}:00Z"))

    def test_btc_has_two_day_feature_lag_and_pre_entry_decision(self):
        frame = self.frame(crypto=True)
        dataset = build_timing_dataset(frame, "BTC-USD", self.context())
        row = dataset.loc["2026-09-10"]
        self.assertEqual(row["feature_date"], pd.Timestamp("2026-09-08T00:00:00Z"))
        self.assertEqual(row["feature_available_at"], pd.Timestamp("2026-09-09T00:30:00Z"))
        self.assertEqual(row["decision_at"], row["feature_available_at"])
        self.assertEqual(row["entry_at"], pd.Timestamp("2026-09-10T00:00:00Z"))
        self.assertEqual(row["exit_at"], pd.Timestamp("2026-09-11T00:00:00Z"))
        self.assertEqual(row["label_available_at"], pd.Timestamp("2026-09-11T00:30:00Z"))
        _, raw = _raw_features(frame)
        np.testing.assert_allclose(row[FEATURE_COLUMNS].to_numpy(dtype=float),
                                   raw.loc["2026-09-08", FEATURE_COLUMNS].to_numpy(dtype=float))
        target = frame.loc["2026-09-10"]
        self.assertAlmostEqual(row["target_return"], target["close"] / target["open"] - 1)

    def test_btc_intervening_bar_and_future_context_cannot_change_features(self):
        frame = self.frame(crypto=True)
        context = self.context()
        before = build_timing_dataset(frame, "BTC-USD", context).loc["2026-09-10"]
        frame.loc[frame.index >= pd.Timestamp("2026-09-09", tz="UTC")] *= 2
        for bars in context.values():
            bars.loc[bars.index >= pd.Timestamp("2026-09-09", tz="UTC")] *= 3
        after = build_timing_dataset(frame, "BTC-USD", context).loc["2026-09-10"]
        pd.testing.assert_series_equal(before[ALL_FEATURE_COLUMNS], after[ALL_FEATURE_COLUMNS])

    def test_equity_target_and_later_context_do_not_change_own_features(self):
        frame = self.frame()
        context = self.context()
        before = build_timing_dataset(frame, "SPY", context).loc["2026-09-10"]
        frame.loc[frame.index >= pd.Timestamp("2026-09-10", tz="UTC")] *= 2
        for bars in context.values():
            bars.loc[bars.index >= pd.Timestamp("2026-09-10", tz="UTC")] *= 3
        after = build_timing_dataset(frame, "SPY", context).loc["2026-09-10"]
        pd.testing.assert_series_equal(before[ALL_FEATURE_COLUMNS], after[ALL_FEATURE_COLUMNS])

    def test_missing_exact_feature_dates_never_substitute_an_older_bar(self):
        equity = self.frame().drop(pd.Timestamp("2026-09-04", tz="UTC"))
        result = build_timing_dataset(equity, "SPY", self.context())
        self.assertNotIn(pd.Timestamp("2026-09-08", tz="UTC"), result.index)
        crypto = self.frame(crypto=True).drop(pd.Timestamp("2026-09-08", tz="UTC"))
        result = build_timing_dataset(crypto, "BTC-USD", self.context())
        self.assertNotIn(pd.Timestamp("2026-09-10", tz="UTC"), result.index)

    def test_missing_lookback_dates_cannot_turn_one_session_return_into_two(self):
        frame = self.frame(crypto=True).drop(pd.Timestamp("2026-09-05", tz="UTC"))
        result = build_timing_dataset(frame, "BTC-USD", self.context())
        self.assertNotIn(pd.Timestamp("2026-09-10", tz="UTC"), result.index)
        self.assertIn(pd.Timestamp("2026-09-28", tz="UTC"), result.index)

    def test_all_outputs_are_finite_sorted_and_have_strict_timing(self):
        for symbol, crypto in (("SPY", False), ("BTC-USD", True)):
            dataset = build_timing_dataset(self.frame(crypto=crypto).iloc[::-1], symbol, self.context())
            self.assertFalse(dataset.empty)
            self.assertEqual(dataset.columns.tolist(), OUTPUT_COLUMNS)
            self.assertTrue(dataset.index.is_monotonic_increasing)
            self.assertFalse(dataset.index.has_duplicates)
            self.assertTrue(np.isfinite(dataset[ALL_FEATURE_COLUMNS + ["target_return"]]).all().all())
            self.assertFalse(dataset[TIMING_COLUMNS].isna().any().any())
            self.assertTrue((dataset["feature_available_at"] <= dataset["decision_at"]).all())
            self.assertTrue((dataset["decision_at"] < dataset["entry_at"]).all())
            self.assertTrue((dataset["entry_at"] < dataset["exit_at"]).all())
            self.assertTrue((dataset["exit_at"] < dataset["label_available_at"]).all())
            self.assertEqual(set(dataset["target_contract_version"]), {TARGET_CONTRACT_VERSION})
            for column in TIMING_COLUMNS:
                self.assertEqual(str(dataset[column].dtype), "datetime64[ns, UTC]")

    def test_calendar_includes_pre_2006_history(self):
        frame = self.frame("1992-11-02", "1993-03-05")
        result = build_timing_dataset(frame, "SPY", self.context("1992-11-02", "1993-03-05"))
        self.assertIn(pd.Timestamp("1993-02-01", tz="UTC"), result.index)
        self.assertEqual(result.loc["1993-02-01", "feature_date"], pd.Timestamp("1993-01-29", tz="UTC"))

    def test_unsupported_crypto_and_invalid_ohlc_or_dates_are_rejected(self):
        frame = self.frame()
        with self.assertRaisesRegex(ValueError, "BTC-USD only"):
            build_timing_dataset(frame, "ETH-USD", self.context())
        invalid = frame.copy()
        invalid.iloc[-1, invalid.columns.get_loc("high")] = 1
        with self.assertRaisesRegex(ValueError, "OHLC bounds"):
            build_timing_dataset(invalid, "SPY", self.context())
        invalid = frame.copy()
        dates = list(invalid.index)
        dates[-1] = pd.NaT
        invalid.index = pd.DatetimeIndex(dates)
        with self.assertRaisesRegex(ValueError, "valid session dates"):
            build_timing_dataset(invalid, "SPY", self.context())

    def test_warmup_and_empty_data_return_a_full_empty_schema(self):
        frame = self.frame().head(10)
        for current in (frame, frame.iloc[:0]):
            result = build_timing_dataset(current, "SPY", self.context())
            self.assertTrue(result.empty)
            self.assertEqual(result.columns.tolist(), OUTPUT_COLUMNS)


if __name__ == "__main__":
    unittest.main()
