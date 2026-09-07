"""Raw-observation quality regressions; no network, credentials or orders."""

import copy
import json
import unittest

import pandas as pd

from quantpaper.marketdata.quality import analyze_symbol


class MarketDataQualityTests(unittest.TestCase):
    def setUp(self):
        self.start = pd.Timestamp("2026-09-04T13:30:00Z")
        self.end = pd.Timestamp("2026-09-04T13:35:00Z")
        self.entry = self.start
        self.exit = self.end - pd.Timedelta(minutes=1)

    def fetch(self, records, start, end, feed="iex", complete=True):
        return {"records": records, "complete": complete, "feed": feed,
                "observed_at": "2026-09-05T12:00:00Z", "start": start.isoformat(),
                "end": end.isoformat(), "pages": 1}

    def quote(self, at, bid=99.0, ask=101.0, bid_size=1, ask_size=2):
        return {"t": at.isoformat(), "bp": bid, "ap": ask, "bs": bid_size, "as": ask_size,
                "bx": "V", "ax": "V", "c": ["R"]}

    def bar(self, at, **changes):
        return {"t": at.isoformat(), "o": 100, "h": 102, "l": 99, "c": 101,
                "v": 10, "n": 1, "vw": 100.5, **changes}

    def sources(self):
        bars = self.fetch([self.bar(at) for at in pd.date_range(self.start, self.end, freq="min", inclusive="left")],
                          self.start, self.end)
        entry = self.fetch([self.quote(self.entry)], self.entry, self.entry + pd.Timedelta(minutes=1))
        exit_ = self.fetch([self.quote(self.exit, 109, 111)], self.exit, self.exit + pd.Timedelta(minutes=1))
        return bars, entry, exit_

    def analyze(self, sources=None, symbol="SPY"):
        return analyze_symbol(symbol, *(sources or self.sources()), session_start=self.start,
                              session_end=self.end, entry_at=self.entry, exit_at=self.exit)

    def test_touch_pair_and_fee_stress_are_explicit_diagnostics(self):
        result = self.analyze()
        pair = result["cost_pair"]
        self.assertAlmostEqual(pair["gross_mid_return"], 0.1)
        self.assertAlmostEqual(pair["touch_long_return"], 109 / 101 - 1)
        self.assertAlmostEqual(pair["spread_drag_bps"], (0.1 - (109 / 101 - 1)) * 10000)
        self.assertAlmostEqual(pair["approximate_round_trip_spread_bps"], (200 + 2 / 110 * 10000) / 2)
        self.assertEqual([scenario["assumed_round_trip_fee_bps"] for scenario in result["fee_stress_scenarios"]],
                         [0, 5, 10, 25, 50])
        self.assertIn("not NBBO", result["feed_scope"])
        self.assertTrue(result["research_only"])
        self.assertFalse(result["executable_backtest"])
        self.assertFalse(result["execution_enabled"])

    def test_empty_bars_have_zero_coverage_without_fabricated_minutes(self):
        sources = self.sources()
        sources[0]["records"] = []
        result = self.analyze(sources)
        quality = result["bar_quality"]
        self.assertEqual(quality["expected_minutes"], 5)
        self.assertEqual(quality["valid_count"], 0)
        self.assertEqual(quality["missing_count"], 5)
        self.assertEqual(quality["coverage_ratio"], 0)
        self.assertEqual(len(quality["missing_minutes"]), 5)

    def test_half_open_bars_exact_minutes_and_zero_volume(self):
        sources = self.sources()
        sources[0]["records"] = [self.bar(self.start, v=0, n=0, vw=0),
                                  self.bar(self.start + pd.Timedelta(seconds=1)), self.bar(self.end)]
        quality = self.analyze(sources)["bar_quality"]
        self.assertEqual(quality["valid_count"], 1)
        self.assertEqual(quality["zero_volume_count"], 1)
        self.assertEqual(quality["invalid_count"], 1)
        self.assertEqual(quality["outside_session_count"], 1)
        self.assertEqual(quality["missing_count"], 4)

    def test_duplicate_bars_idempotent_but_conflicts_remove_minute(self):
        sources = self.sources()
        sources[0]["records"].append(copy.deepcopy(sources[0]["records"][0]))
        quality = self.analyze(sources)["bar_quality"]
        self.assertEqual(quality["valid_count"], 5)
        self.assertEqual(quality["identical_duplicate_count"], 1)
        sources[0]["records"].append(self.bar(self.start, c=102))
        quality = self.analyze(sources)["bar_quality"]
        self.assertEqual(quality["valid_count"], 4)
        self.assertEqual(quality["conflicting_minute_count"], 1)
        self.assertEqual(quality["conflicting_record_count"], 3)
        self.assertEqual(quality["invalid_count"], 3)
        self.assertEqual(quality["missing_count"], 1)

    def test_stale_and_after_deadline_quotes_never_selected(self):
        sources = self.sources()
        sources[1]["records"] = [self.quote(self.entry - pd.Timedelta(seconds=1)),
                                  self.quote(self.entry + pd.Timedelta(seconds=31))]
        result = self.analyze(sources)
        self.assertIsNone(result["cost_pair"])
        self.assertIsNone(result["entry_window"]["selected_quote"])
        self.assertEqual(result["entry_window"]["before_target_count"], 1)
        self.assertEqual(result["entry_window"]["after_deadline_count"], 1)

    def test_next_valid_quote_chosen_in_interval_not_before_target(self):
        sources = self.sources()
        sources[1]["records"] = [self.quote(self.entry - pd.Timedelta(seconds=1)),
                                  self.quote(self.entry, 101, 100),
                                  self.quote(self.entry + pd.Timedelta(seconds=7)),
                                  self.quote(self.entry + pd.Timedelta(seconds=4))]
        window = self.analyze(sources)["entry_window"]
        self.assertEqual(window["selected_quote"]["delay_seconds"], 4)
        self.assertEqual(window["invalid_count"], 1)

    def test_wait_limit_inclusive_preserves_nanosecond_boundary(self):
        sources = self.sources()
        sources[1]["records"] = [self.quote(self.entry + pd.Timedelta(seconds=30))]
        self.assertIsNotNone(self.analyze(sources)["cost_pair"])
        sources[1]["records"] = [self.quote(self.entry + pd.Timedelta(seconds=30, nanoseconds=1))]
        self.assertIsNone(self.analyze(sources)["cost_pair"])

    def test_quote_duplicate_idempotence_and_ambiguous_same_timestamp(self):
        sources = self.sources()
        sources[1]["records"].append(copy.deepcopy(sources[1]["records"][0]))
        result = self.analyze(sources)
        self.assertIsNotNone(result["cost_pair"])
        self.assertEqual(result["entry_window"]["identical_duplicate_count"], 1)
        sources[1]["records"].append(self.quote(self.entry, 98, 102))
        result = self.analyze(sources)
        self.assertIsNone(result["cost_pair"])
        self.assertTrue(result["entry_window"]["ambiguous_first_quote"])
        self.assertEqual(result["entry_window"]["valid_count"], 2)
        self.assertEqual(result["entry_window"]["first_timestamp_quote_count"], 2)

    def test_wide_spreads_reported_event_weighted_over_full_fetch_window(self):
        sources = self.sources()
        sources[1]["records"] = [self.quote(self.entry, 99, 101),
                                  self.quote(self.entry + pd.Timedelta(seconds=10), 95, 105),
                                  self.quote(self.entry + pd.Timedelta(seconds=40), 90, 110),
                                  self.quote(self.entry + pd.Timedelta(seconds=60), 1, 199)]
        window = self.analyze(sources)["entry_window"]
        stats = window["spread_bps_distribution"]
        self.assertEqual(stats["count"], 3)
        self.assertAlmostEqual(stats["mean"], (200 + 1000 + 2000) / 3)
        self.assertEqual(stats["max"], 2000)
        self.assertEqual(window["timely_valid_quote_count"], 2)
        self.assertEqual(window["selected_quote"]["delay_seconds"], 0)

    def test_crossed_zero_nonfinite_and_naive_quotes_fail_without_nan_json(self):
        sources = self.sources()
        invalid = [self.quote(self.entry, 101, 100), self.quote(self.entry, 0, 100),
                   self.quote(self.entry, ask_size=0), self.quote(self.entry, bid=float("nan")),
                   self.quote(self.entry, ask=float("inf")), self.quote(self.entry)]
        invalid[-1]["t"] = "2026-09-04T13:30:00"
        sources[1]["records"] = invalid
        result = self.analyze(sources)
        self.assertIsNone(result["cost_pair"])
        self.assertEqual(result["entry_window"]["invalid_count"], 6)
        self.assertIsNone(result["entry_window"]["spread_bps_distribution"]["mean"])
        json.dumps(result, allow_nan=False)

    def test_timezone_offsets_normalize_to_same_absolute_quote_time(self):
        sources = self.sources()
        sources[1]["records"][0]["t"] = "2026-09-04T09:30:00-04:00"
        result = self.analyze(sources)
        self.assertIsNotNone(result["cost_pair"])
        self.assertEqual(result["entry_window"]["selected_quote"]["timestamp"], self.entry.isoformat())

    def test_any_incomplete_source_blocks_quote_pair(self):
        for position in range(3):
            sources = self.sources()
            sources[position]["complete"] = False
            result = self.analyze(sources)
            self.assertFalse(result["complete"])
            self.assertIsNone(result["cost_pair"])
            self.assertEqual(result["fee_stress_scenarios"], [])

    def test_unknown_or_cross_feed_blocks_quote_pair(self):
        sources = self.sources()
        sources[1]["feed"] = "sip"
        result = self.analyze(sources)
        self.assertIsNone(result["cost_pair"])
        self.assertIn("cross_feed_quote_pair_forbidden", result["reasons"])
        for fetch in sources:
            fetch["feed"] = "unknown"
        self.assertIsNone(self.analyze(sources)["cost_pair"])

    def test_crypto_us_is_only_allowed_for_btc_slash_usd(self):
        sources = self.sources()
        for fetch in sources:
            fetch["feed"] = "crypto_us"
        result = self.analyze(sources, symbol="BTC/USD")
        self.assertIsNotNone(result["cost_pair"])
        self.assertIn("crypto venue", result["feed_scope"])
        self.assertIsNone(self.analyze(sources, symbol="SPY")["cost_pair"])
        self.assertIsNone(self.analyze(symbol="BTC/USD")["cost_pair"])

    def test_invalid_bars_flagged_and_missing_window_blocks_pair(self):
        sources = self.sources()
        sources[0]["records"] = [self.bar(self.start, c=float("nan")), self.bar(self.start, h=1),
                                  self.bar(self.start, v=-1), self.bar(self.start, n=1.5)]
        sources[2]["records"] = []
        result = self.analyze(sources)
        self.assertEqual(result["bar_quality"]["invalid_count"], 4)
        self.assertIsNone(result["cost_pair"])
        self.assertIn("exit_has_no_valid_timely_quote", result["reasons"])

    def test_wrong_declared_or_per_record_symbol_blocks_standalone_pair(self):
        sources = self.sources()
        sources[1]["symbol"] = "JPM"
        result = self.analyze(sources)
        self.assertIsNone(result["cost_pair"])
        self.assertTrue(result["complete"])
        self.assertFalse(result["source_metadata_valid"])
        sources = self.sources()
        sources[2]["records"][0]["S"] = "XOM"
        result = self.analyze(sources)
        self.assertIsNone(result["cost_pair"])
        self.assertIn("exit_quotes_record_declares_wrong_symbol", result["reasons"])

    def test_late_start_or_short_fetch_cannot_claim_first_quote_in_window(self):
        sources = self.sources()
        sources[1]["start"] = (self.entry + pd.Timedelta(seconds=5)).isoformat()
        sources[1]["records"] = [self.quote(self.entry + pd.Timedelta(seconds=6))]
        result = self.analyze(sources)
        self.assertIsNone(result["cost_pair"])
        self.assertEqual(result["entry_window"]["selected_quote"]["delay_seconds"], 6)
        sources = self.sources()
        sources[2]["end"] = (self.exit + pd.Timedelta(seconds=15)).isoformat()
        self.assertIsNone(self.analyze(sources)["cost_pair"])

    def test_malformed_or_future_fetch_metadata_blocks_cost_but_not_pagination_flag(self):
        sources = self.sources()
        sources[0]["observed_at"] = (self.start - pd.Timedelta(days=1)).isoformat()
        result = self.analyze(sources)
        self.assertIsNone(result["cost_pair"])
        self.assertTrue(result["complete"])
        sources = self.sources()
        sources[1]["kind"] = "bars"
        sources[1]["start"] = "not-a-time"
        result = self.analyze(sources)
        self.assertIsNone(result["cost_pair"])
        self.assertFalse(result["source_metadata_valid"])
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
