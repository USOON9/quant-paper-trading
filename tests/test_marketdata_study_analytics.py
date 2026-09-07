"""Offline, source-provenance and equal-day market-data study regressions."""

import copy
import json
import unittest

import pandas as pd

from quantpaper.marketdata.study_analytics import analyze_observation, aggregate_observations


class StudyAnalyticsTests(unittest.TestCase):
    def sources(self, *, date="2026-09-04", symbol="SPY", feed="sip", spread=2):
        start = pd.Timestamp(f"{date}T13:30:00Z")
        end = start + pd.Timedelta(minutes=6)
        targets = {name: start + pd.Timedelta(minutes=index * 2) for index, name in enumerate(("opening", "midday", "closing"))}
        bars = self.fetch(symbol, "bars", feed, start, end, [
            {"t": at.isoformat(), "o": 100, "h": 102, "l": 98, "c": 101, "v": 1, "n": 1}
            for at in pd.date_range(start, end, freq="min", inclusive="left")])
        quotes = {name: self.fetch(symbol, "quotes", feed, target, target + pd.Timedelta(seconds=60), [
            self.quote(target + pd.Timedelta(milliseconds=offset), 100 - spread / 2, 100 + spread / 2)
            for offset in (0, 250, 1000)]) for name, target in targets.items()}
        return symbol, bars, quotes, {"session_start": start, "session_end": end, "targets": targets}

    @staticmethod
    def fetch(symbol, kind, feed, start, end, records):
        return {"symbol": symbol, "kind": kind, "feed": feed, "start": start.isoformat(),
                "end": end.isoformat(), "observed_at": "2026-09-07T12:00:00Z", "records": records,
                "complete": True, "pages": 1}

    @staticmethod
    def quote(at, bid=99, ask=101, **changes):
        return {"t": at.isoformat(), "bp": bid, "ap": ask, "bs": 1, "as": 1, **changes}

    def analyze(self, sources=None):
        symbol, bars, quotes, kwargs = sources or self.sources()
        return analyze_observation(symbol, bars, quotes, **kwargs)

    def test_happy_path_is_json_safe_and_contains_no_fees_or_returns(self):
        result = self.analyze()
        self.assertTrue(result["complete"])
        self.assertTrue(result["source_metadata_valid"])
        self.assertTrue(result["bar_source_usable"])
        self.assertEqual(result["bar_quality"]["valid_count"], 6)
        self.assertAlmostEqual(result["opening_closing_spread_pair"]["approximate_round_trip_spread_bps"], 200)
        for window in result["windows"].values():
            self.assertTrue(window["usable"])
            self.assertTrue(window["distribution_usable"])
            self.assertEqual([item["latency_ms"] for item in window["latency_scenarios"]], [0, 250, 1000])
        serialized = json.dumps(result, allow_nan=False)
        self.assertNotIn('"gross_mid_return"', serialized)
        self.assertNotIn('"fee_stress_scenarios"', serialized)
        self.assertFalse(result["execution_enabled"])

    def test_failed_empty_rows_stay_in_expected_counts(self):
        good = self.analyze()
        source = self.sources(date="2026-09-03")
        for fetch in [source[1], *source[2].values()]:
            fetch.update(records=[], complete=False, pages=0)
        failed = self.analyze(source)
        summary = aggregate_observations([good, failed])["by_symbol"]["SPY"]
        self.assertEqual(summary["expected_sessions"], 2)
        self.assertEqual(summary["usable_complete_sessions"], 1)
        self.assertEqual(summary["minute_coverage"]["expected_minutes"], 12)
        self.assertEqual(summary["minute_coverage"]["source_usable_valid_minutes"], 6)
        for window in summary["by_window"].values():
            self.assertEqual(window["expected_samples"], 2)
            self.assertEqual(window["usable_selected_samples"], 1)
            self.assertEqual(window["missing_window_count"], 1)
            self.assertEqual(window["incomplete_or_truncated_window_count"], 1)

    def test_day_statistics_are_equal_weight_not_raw_quote_weighted(self):
        first = self.analyze(self.sources(date="2026-09-03", spread=2))
        second_source = self.sources(date="2026-09-04", spread=10)
        for name, fetch in second_source[2].items():
            target = second_source[3]["targets"][name]
            fetch["records"] = [self.quote(target + pd.Timedelta(milliseconds=i), 95, 105) for i in range(2000)]
        second = self.analyze(second_source)
        result = aggregate_observations([first, second])["by_symbol"]["SPY"]
        for window in result["by_window"].values():
            for field in ("selected_full_spread_bps", "daily_window_event_median_spread_bps"):
                self.assertEqual(window[field]["count"], 2)
                self.assertEqual(window[field]["median"], 600)
                self.assertEqual(window[field]["p90"], 920)
                self.assertEqual(window[field]["max"], 1000)
        self.assertEqual(result["opening_closing_spread_pair"]["approximate_round_trip_spread_bps"]["median"], 600)

    def test_missing_and_wrong_symbol_or_kind_are_fail_closed(self):
        for field, value in (("symbol", "JPM"), ("kind", "bars"), ("symbol", None), ("kind", None)):
            with self.subTest(field=field, value=value):
                source = self.sources()
                if value is None:
                    del source[2]["midday"][field]
                else:
                    source[2]["midday"][field] = value
                result = self.analyze(source)
                self.assertFalse(result["source_metadata_valid"])
                self.assertFalse(result["windows"]["midday"]["usable"])
                self.assertFalse(result["windows"]["midday"]["distribution_usable"])
                self.assertTrue(result["windows"]["opening"]["usable"])

    def test_wrong_record_symbol_is_fail_closed(self):
        source = self.sources()
        source[2]["opening"]["records"][0]["S"] = "JPM"
        result = self.analyze(source)
        self.assertFalse(result["windows"]["opening"]["usable"])
        self.assertIsNone(result["opening_closing_spread_pair"])

    def test_unknown_and_mixed_window_feeds_fail_closed(self):
        for feed in ("iex", "unknown", "crypto_us", None, ["sip"]):
            source = self.sources()
            source[2]["midday"]["feed"] = feed
            result = self.analyze(source)
            self.assertFalse(result["windows"]["midday"]["usable"])
            self.assertFalse(result["windows"]["midday"]["distribution_usable"])
            self.assertTrue(result["windows"]["opening"]["usable"])

    def test_incomplete_quote_window_has_no_usable_latency_or_distribution(self):
        source = self.sources()
        source[2]["opening"]["complete"] = False
        result = self.analyze(source)
        self.assertFalse(result["complete"])
        self.assertTrue(result["windows"]["opening"]["quality"]["selected_quote"] is not None)
        self.assertFalse(result["windows"]["opening"]["distribution_usable"])
        self.assertTrue(all(not item["available"] for item in result["windows"]["opening"]["latency_scenarios"]))
        self.assertIsNone(result["opening_closing_spread_pair"])

    def test_no_timely_quote_can_still_have_complete_window_distribution(self):
        source = self.sources()
        target = source[3]["targets"]["midday"]
        source[2]["midday"]["records"] = [self.quote(target + pd.Timedelta(seconds=45))]
        window = self.analyze(source)["windows"]["midday"]
        self.assertFalse(window["usable"])
        self.assertTrue(window["distribution_usable"])
        self.assertTrue(all(not item["available"] for item in window["latency_scenarios"]))
        summary = aggregate_observations([self.analyze(source)])["by_symbol"]["SPY"]["by_window"]["midday"]
        self.assertEqual(summary["usable_selected_samples"], 0)
        self.assertEqual(summary["usable_distribution_samples"], 1)
        self.assertEqual(summary["no_timely_valid_quote_window_count"], 1)

    def test_unsequenced_first_timestamp_is_not_skipped_for_later_quote(self):
        source = self.sources()
        target = source[3]["targets"]["opening"]
        source[2]["opening"]["records"].append(self.quote(target, 98, 102))
        window = self.analyze(source)["windows"]["opening"]
        self.assertFalse(window["usable"])
        self.assertTrue(window["distribution_usable"])
        self.assertTrue(window["latency_scenarios"][1]["available"])
        self.assertFalse(window["latency_scenarios"][1]["paired_with_zero_latency"])
        self.assertIsNone(window["latency_scenarios"][1]["ask_price_displacement_bps_vs_zero"])

    def test_identical_quote_duplicates_do_not_make_first_timestamp_ambiguous(self):
        source = self.sources()
        source[2]["opening"]["records"].append(copy.deepcopy(source[2]["opening"]["records"][0]))
        window = self.analyze(source)["windows"]["opening"]
        self.assertTrue(window["usable"])
        self.assertEqual(window["quality"]["identical_duplicate_count"], 1)

    def test_latency_shift_and_signed_price_displacement_are_paired_within_window(self):
        source = self.sources()
        target = source[3]["targets"]["opening"]
        source[2]["opening"]["records"] = [self.quote(target, 99, 101),
            self.quote(target + pd.Timedelta(milliseconds=250), 100, 102),
            self.quote(target + pd.Timedelta(seconds=1), 98, 100)]
        scenarios = self.analyze(source)["windows"]["opening"]["latency_scenarios"]
        self.assertAlmostEqual(scenarios[1]["ask_price_displacement_bps_vs_zero"], (102 / 101 - 1) * 10000)
        self.assertAlmostEqual(scenarios[1]["bid_price_displacement_bps_vs_zero"], (100 / 99 - 1) * 10000)
        self.assertAlmostEqual(scenarios[2]["ask_price_displacement_bps_vs_zero"], (100 / 101 - 1) * 10000)
        self.assertEqual(scenarios[1]["wait_after_shift_seconds"], 0)
        self.assertEqual(scenarios[1]["event_time_gap_ms_vs_zero"], 250)
        self.assertEqual(scenarios[2]["event_time_gap_ms_vs_zero"], 1000)
        self.assertTrue(all(item["paired_with_zero_latency"] for item in scenarios))
        summary = aggregate_observations([self.analyze(source)])["by_symbol"]["SPY"]["by_window"]["opening"]["latency_scenarios"][2]
        self.assertLess(summary["bid_price_displacement_bps_vs_zero"]["min"], 0)
        self.assertGreater(summary["bid_price_displacement_bps_vs_zero"]["absolute_max"], 0)

    def test_wait_cap_is_inclusive_for_each_shift_with_nanosecond_precision(self):
        source = self.sources()
        target = source[3]["targets"]["opening"]
        source[2]["opening"]["records"] = [self.quote(target + pd.Timedelta(seconds=30, milliseconds=250))]
        scenarios = self.analyze(source)["windows"]["opening"]["latency_scenarios"]
        self.assertFalse(scenarios[0]["available"])
        self.assertTrue(scenarios[1]["available"])
        self.assertTrue(scenarios[2]["available"])
        self.assertEqual(scenarios[1]["wait_after_shift_seconds"], 30)
        source[2]["opening"]["records"] = [self.quote(target + pd.Timedelta(seconds=31, nanoseconds=1))]
        self.assertTrue(all(not item["available"] for item in self.analyze(source)["windows"]["opening"]["latency_scenarios"]))

    def test_later_availability_without_zero_baseline_is_not_in_paired_aggregate(self):
        source = self.sources()
        target = source[3]["targets"]["opening"]
        source[2]["opening"]["records"] = [self.quote(target + pd.Timedelta(seconds=30, milliseconds=500))]
        summary = aggregate_observations([self.analyze(source)])["by_symbol"]["SPY"]["by_window"]["opening"]["latency_scenarios"]
        self.assertEqual(summary[2]["available_samples"], 1)
        self.assertEqual(summary[2]["paired_with_zero_samples"], 0)
        self.assertEqual(summary[2]["ask_price_displacement_bps_vs_zero"]["count"], 0)
        self.assertIsNone(summary[2]["ask_price_displacement_bps_vs_zero"]["median"])

    def test_malformed_ranges_short_or_long_windows_and_observation_time_are_unusable(self):
        for field, offset in (("start", 1), ("end", -1), ("end", 1)):
            source = self.sources()
            fetch = source[2]["opening"]
            fetch[field] = (pd.Timestamp(fetch[field]) + pd.Timedelta(seconds=offset)).isoformat()
            self.assertFalse(self.analyze(source)["windows"]["opening"]["usable"])
        source = self.sources()
        source[2]["opening"]["observed_at"] = "2026-09-04T00:00:00Z"
        self.assertFalse(self.analyze(source)["windows"]["opening"]["usable"])

    def test_crossed_nonfinite_zero_size_quotes_are_excluded_and_counted(self):
        source = self.sources()
        target = source[3]["targets"]["midday"]
        source[2]["midday"]["records"] += [self.quote(target, 101, 100),
            self.quote(target, float("nan"), 100), self.quote(target, **{"as": 0})]
        row = self.analyze(source)
        self.assertTrue(row["windows"]["midday"]["usable"])
        summary = aggregate_observations([row])["by_symbol"]["SPY"]["by_window"]["midday"]
        self.assertEqual(summary["invalid_quote_window_count"], 1)
        self.assertEqual(summary["invalid_quote_record_count"], 3)
        json.dumps(row, allow_nan=False)

    def test_missing_bars_and_zero_volume_are_counted_without_filling(self):
        source = self.sources()
        source[1]["records"].pop()
        source[1]["records"][0].update(v=0, n=0)
        row = self.analyze(source)
        self.assertEqual(row["bar_quality"]["missing_count"], 1)
        self.assertEqual(row["bar_quality"]["zero_volume_count"], 1)
        summary = aggregate_observations([row])["by_symbol"]["SPY"]
        self.assertEqual(summary["all_minutes_present_sessions"], 0)
        self.assertEqual(summary["minute_coverage"]["source_usable_valid_minutes"], 5)
        self.assertIsNotNone(row["opening_closing_spread_pair"])

    def test_bad_bar_provenance_blocks_bar_coverage_and_pair_but_not_valid_quote_windows(self):
        source = self.sources()
        del source[1]["symbol"]
        row = self.analyze(source)
        self.assertFalse(row["bar_source_usable"])
        self.assertTrue(row["windows"]["opening"]["usable"])
        self.assertIsNone(row["opening_closing_spread_pair"])
        coverage = aggregate_observations([row])["by_symbol"]["SPY"]["minute_coverage"]
        self.assertEqual(coverage["observed_valid_minutes_unfiltered_for_source"], 6)
        self.assertEqual(coverage["source_usable_valid_minutes"], 0)

    def test_symbols_are_never_pooled_and_crypto_feed_is_venue_scoped(self):
        stock = self.analyze()
        crypto = self.analyze(self.sources(symbol="BTC/USD", feed="crypto_us", spread=10))
        summary = aggregate_observations([stock, crypto])
        self.assertEqual(set(summary["by_symbol"]), {"SPY", "BTC/USD"})
        self.assertEqual(summary["by_symbol"]["SPY"]["expected_sessions"], 1)
        self.assertIn("not a universal", crypto["feed_scope"])

    def test_duplicate_symbol_session_rows_rejected_and_mixed_feeds_not_pooled(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            aggregate_observations([self.analyze(), self.analyze()])
        result = aggregate_observations([self.analyze(), self.analyze(self.sources(date="2026-09-03", feed="iex"))])["by_symbol"]["SPY"]
        self.assertFalse(result["pooling_allowed"])
        self.assertEqual(result["expected_sessions"], 2)
        self.assertEqual(result["by_window"]["opening"]["usable_selected_samples"], 0)

    def test_exact_window_names_order_and_session_fit_required(self):
        source = self.sources()
        del source[2]["midday"]
        with self.assertRaises(ValueError):
            self.analyze(source)
        source = self.sources()
        source[3]["targets"]["midday"] = source[3]["targets"]["closing"]
        with self.assertRaises(ValueError):
            self.analyze(source)
        source = self.sources()
        source[3]["targets"]["closing"] = source[3]["session_end"] - pd.Timedelta(seconds=30)
        with self.assertRaises(ValueError):
            self.analyze(source)

    def test_empty_aggregate_is_json_safe(self):
        summary = aggregate_observations([])
        self.assertEqual(summary["observation_count"], 0)
        self.assertEqual(summary["by_symbol"], {})
        json.dumps(summary, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
