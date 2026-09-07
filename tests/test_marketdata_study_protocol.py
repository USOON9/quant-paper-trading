from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest

from quantpaper.marketdata.study_protocol import SYMBOLS, build_study_plan


UTC = timezone.utc


class MarketDataStudyProtocolTests(unittest.TestCase):
    def test_labor_day_default_five_distinct_days_and_exact_budget(self):
        plan = build_study_plan(now=datetime(2026, 9, 7, 10, tzinfo=UTC))
        self.assertEqual(plan["contract"], "intraday_multi_session_v1")
        self.assertEqual(plan["symbols"], list(SYMBOLS))
        self.assertEqual(plan["summary"], {
            "expected_observations": 30,
            "expected_request_segments": 120,
            "max_planned_pages": 360,
            "sample_days_by_asset": {
                "stocks": ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"],
                "crypto": ["2026-09-02", "2026-09-03", "2026-09-04", "2026-09-05", "2026-09-06"],
            },
        })
        self.assertFalse(plan["execution_enabled"])
        self.assertTrue(plan["research_only"])
        for symbol in SYMBOLS:
            dates = [row["session"] for row in plan["observations"] if row["symbol"] == symbol]
            self.assertEqual(len(dates), 5)
            self.assertEqual(dates, sorted(set(dates)))

    def test_stock_and_crypto_targets_have_explicit_feeds_and_bounds(self):
        plan = build_study_plan(now=datetime(2026, 9, 7, 10, tzinfo=UTC), sessions=1)
        spy, btc = plan["observations"][0], plan["observations"][-1]
        self.assertEqual(spy, {
            "symbol": "SPY", "session": "2026-09-04", "feed": "sip",
            "session_start": "2026-09-04T13:30:00Z", "session_end": "2026-09-04T20:00:00Z",
            "targets": {"opening": "2026-09-04T13:35:00Z", "midday": "2026-09-04T16:45:00Z",
                        "closing": "2026-09-04T19:55:00Z"},
        })
        self.assertEqual(btc["feed"], "crypto_us")
        self.assertEqual(btc["targets"], {
            "opening": "2026-09-06T00:35:00Z", "midday": "2026-09-06T12:00:00Z",
            "closing": "2026-09-06T23:55:00Z",
        })
        self.assertEqual(plan["timing"]["latency_scenarios_ms"], [0, 250, 1000])
        self.assertEqual(plan["timing"]["quote_window_seconds"], 60)
        self.assertEqual(plan["timing"]["max_quote_wait_seconds"], 30)

    def test_true_session_midpoint_and_early_close(self):
        plan = build_study_plan(now=datetime(2026, 11, 28, 10, tzinfo=UTC), sessions=1, symbols=("SPY",))
        row = plan["observations"][0]
        self.assertEqual(row["session"], "2026-11-27")
        self.assertEqual(row["session_end"], "2026-11-27T18:00:00Z")
        self.assertEqual(row["targets"], {
            "opening": "2026-11-27T14:35:00Z", "midday": "2026-11-27T16:15:00Z",
            "closing": "2026-11-27T17:55:00Z",
        })

    def test_dst_changes_utc_session_boundaries(self):
        plan = build_study_plan(now=datetime(2026, 3, 10, 10, tzinfo=UTC), sessions=2, symbols=("SPY",))
        friday, monday = plan["observations"]
        self.assertEqual(friday["session_start"], "2026-03-06T14:30:00Z")
        self.assertEqual(monday["session_start"], "2026-03-09T13:30:00Z")
        self.assertEqual(friday["targets"]["midday"], "2026-03-06T17:45:00Z")
        self.assertEqual(monday["targets"]["midday"], "2026-03-09T16:45:00Z")

    def test_crypto_publication_buffer_exact_boundary(self):
        before = build_study_plan(now=datetime(2026, 9, 7, 0, 29, 59, tzinfo=UTC), sessions=1)
        ready = build_study_plan(now=datetime(2026, 9, 7, 0, 30, tzinfo=UTC), sessions=1)
        self.assertEqual(before["summary"]["sample_days_by_asset"]["crypto"], ["2026-09-05"])
        self.assertEqual(ready["summary"]["sample_days_by_asset"]["crypto"], ["2026-09-06"])

    def test_stock_publication_buffer_exact_boundary(self):
        before = build_study_plan(now=datetime(2026, 9, 4, 20, 29, 59, tzinfo=UTC), sessions=1)
        ready = build_study_plan(now=datetime(2026, 9, 4, 20, 30, tzinfo=UTC), sessions=1)
        self.assertEqual(before["summary"]["sample_days_by_asset"]["stocks"], ["2026-09-03"])
        self.assertEqual(ready["summary"]["sample_days_by_asset"]["stocks"], ["2026-09-04"])

    def test_maximum_budget_cannot_exceed_240_segments_or_720_pages(self):
        plan = build_study_plan(now=datetime(2026, 9, 7, 10, tzinfo=UTC), sessions=10)
        self.assertEqual(len(plan["observations"]), 60)
        self.assertEqual(plan["summary"]["expected_request_segments"], 240)
        self.assertEqual(plan["summary"]["max_planned_pages"], 720)
        self.assertEqual(plan["limits"]["max_fetches"], 240)
        self.assertEqual(plan["limits"]["max_pages"], 720)
        self.assertEqual(plan["limits"]["max_pages_per_fetch"], 3)

    def test_non_integer_and_out_of_range_session_counts_rejected(self):
        for sessions in (True, False, 0, -1, 11, 1.0, "5", None):
            with self.subTest(sessions=sessions), self.assertRaises(ValueError):
                build_study_plan(now=datetime(2026, 9, 7, tzinfo=UTC), sessions=sessions)

    def test_symbol_and_feed_rejection(self):
        for symbols in ((), ("SPY", "SPY"), ("AAPL",), ("BTC-USD",), ("spy",), (None,), "SPY", ["SPY"]):
            with self.subTest(symbols=symbols), self.assertRaises(ValueError):
                build_study_plan(now=datetime(2026, 9, 7, tzinfo=UTC), symbols=symbols)
        for feed in ("", "SIP", "otc", "boats", "crypto_us", None, [], True):
            with self.subTest(feed=feed), self.assertRaises(ValueError):
                build_study_plan(now=datetime(2026, 9, 7, tzinfo=UTC), stock_feed=feed)

    def test_subset_and_iex_are_explicit(self):
        plan = build_study_plan(now=datetime(2026, 9, 7, 10, tzinfo=UTC), sessions=1, stock_feed="iex", symbols=("JNJ",))
        self.assertEqual(plan["symbols"], ["JNJ"])
        self.assertEqual(plan["observations"][0]["feed"], "iex")
        self.assertEqual(plan["summary"]["sample_days_by_asset"]["crypto"], [])
        self.assertEqual(plan["summary"]["expected_request_segments"], 4)
        crypto = build_study_plan(now=datetime(2026, 9, 7, 10, tzinfo=UTC), sessions=1, symbols=("BTC/USD",))
        self.assertEqual(crypto["summary"]["sample_days_by_asset"]["stocks"], [])

    def test_clock_is_required_aware_and_json_safe(self):
        for now in (datetime(2026, 9, 7), "2026-09-07", None, 1):
            with self.subTest(now=now), self.assertRaises(ValueError):
                build_study_plan(now=now)
        now = datetime(2026, 9, 7, 11, 30, 1, 123456, tzinfo=timezone(timedelta(hours=1)))
        plan = build_study_plan(now=now)
        self.assertEqual(plan["created_at"], "2026-09-07T10:30:01.123456Z")
        self.assertEqual(plan["cutoff"], "2026-09-07T10:00:01.123456Z")
        self.assertEqual(json.loads(json.dumps(plan, allow_nan=False)), plan)
        self.assertEqual(build_study_plan(now=now), plan)


if __name__ == "__main__":
    unittest.main()
