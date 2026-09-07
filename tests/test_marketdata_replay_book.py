"""Pure historical-state replay regressions; never contacts a broker."""

import copy
import json
import unittest

import pandas as pd

from quantpaper.marketdata.replay_book import analyze_replay_window


class ReplayBookTests(unittest.TestCase):
    target = pd.Timestamp("2026-09-04T13:35:00Z")

    def quote(self, offset_ms=-100, bid=99, ask=101, **changes):
        return {"t": (self.target + pd.Timedelta(milliseconds=offset_ms)).isoformat(),
                "bp": bid, "ap": ask, "bs": 1, "as": 2, "c": ["R"],
                "bx": "V", "ax": "V", "z": "C", **changes}

    def fetch(self, records=None, **changes):
        return {"symbol": "SPY", "kind": "quotes", "feed": "sip", "complete": True,
                "pages": 1, "start": (self.target - pd.Timedelta(seconds=5)).isoformat(),
                "end": (self.target + pd.Timedelta(seconds=2)).isoformat(),
                "observed_at": "2026-09-07T12:00:00Z",
                "records": records if records is not None else [self.quote()], **changes}

    def analyze(self, fetch=None, **kwargs):
        return analyze_replay_window("SPY", fetch if fetch is not None else self.fetch(), target=self.target, **kwargs)

    def test_valid_decision_and_strict_prior_arrivals_are_diagnostic_only(self):
        row = self.analyze(self.fetch([self.quote(-100), self.quote(200, 100, 102), self.quote(900, 101, 103)]))
        self.assertTrue(row["source_usable"])
        self.assertEqual(row["decision_state"]["status"], "VALID")
        self.assertEqual(row["decision_state"]["age_ms"], 100)
        self.assertEqual(row["scenarios"][0]["arrival_state"], row["decision_state"])
        self.assertEqual([item["buy_reference_price"] for item in row["scenarios"]], [101, 102, 103])
        self.assertTrue(all(item["status"] == "QUOTE_ELIGIBLE_NO_FILL_ASSUMED" for item in row["scenarios"]))
        self.assertFalse(row["execution_enabled"])
        self.assertFalse(row["fills_assumed"])
        self.assertEqual(row["decision_state"]["quote"]["quote_conditions"], ["R"])
        self.assertEqual(row["decision_state"]["quote"]["tape"], "C")
        json.dumps(row, allow_nan=False)

    def test_future_clean_events_cannot_change_decision_state(self):
        original = self.analyze()
        future = self.analyze(self.fetch([self.quote(), self.quote(100, 1, 2), self.quote(1100, 1000, 1001)]))
        self.assertEqual(original["decision_state"], future["decision_state"])
        self.assertEqual(original["scenarios"][0], future["scenarios"][0])

    def test_exact_decision_and_arrival_boundary_events_excluded(self):
        row = self.analyze(self.fetch([self.quote(-100, 98, 100), self.quote(0, 99, 101),
                                      self.quote(250, 100, 102), self.quote(1000, 101, 103)]))
        self.assertEqual(row["decision_state"]["quote"]["bid_price"], 98)
        self.assertEqual([item["sell_reference_price"] for item in row["scenarios"]], [98, 99, 100])

    def test_nanosecond_before_boundary_counts_but_exact_does_not(self):
        before = self.quote(t=(self.target - pd.Timedelta(nanoseconds=1)).isoformat())
        exact = self.quote(0, 200, 201)
        row = self.analyze(self.fetch([before, exact]))
        self.assertEqual(row["decision_state"]["age_ms"], .000001)
        self.assertEqual(row["decision_state"]["quote"]["bid_price"], 99)

    def test_invalid_latest_update_poisons_state_without_old_quote_fallback(self):
        row = self.analyze(self.fetch([self.quote(-200), self.quote(-100, 102, 101)]))
        self.assertEqual(row["decision_state"]["status"], "INVALID")
        self.assertIsNone(row["decision_state"]["quote"])
        self.assertIn("crossed_quote", row["decision_state"]["reasons"])
        self.assertEqual(row["counts"]["invalidations"], 1)
        self.assertTrue(all(item["buy_reference_price"] is None for item in row["scenarios"]))

    def test_invalid_arrival_update_blocks_even_when_decision_was_valid(self):
        row = self.analyze(self.fetch([self.quote(-100), self.quote(200, 99, 99)]))
        self.assertEqual(row["scenarios"][0]["status"], "QUOTE_ELIGIBLE_NO_FILL_ASSUMED")
        self.assertEqual(row["scenarios"][1]["status"], "ARRIVAL_BLOCKED")
        self.assertIn("locked_quote", row["scenarios"][1]["arrival_state"]["reasons"])

    def test_strictly_later_clean_update_recovers_state_but_not_blocked_decision(self):
        row = self.analyze(self.fetch([self.quote(-200), self.quote(-100, 102, 101), self.quote(100), self.quote(900)]))
        self.assertEqual(row["decision_state"]["status"], "INVALID")
        self.assertEqual(row["scenarios"][1]["arrival_state"]["status"], "VALID")
        self.assertEqual(row["scenarios"][1]["status"], "DECISION_BLOCKED")
        self.assertIsNone(row["scenarios"][1]["buy_reference_price"])
        self.assertEqual(row["counts"]["recoveries"], 1)

    def test_predecision_recovery_is_usable(self):
        row = self.analyze(self.fetch([self.quote(-300, 102, 101), self.quote(-100)]))
        self.assertEqual(row["decision_state"]["status"], "VALID")
        self.assertEqual(row["counts"]["recoveries"], 1)

    def test_distinct_same_timestamp_updates_ambiguous_until_later_clean_event(self):
        row = self.analyze(self.fetch([self.quote(-100), self.quote(-100, 98, 102), self.quote(100)]))
        self.assertEqual(row["decision_state"]["status"], "INVALID")
        self.assertIn("distinct_same_timestamp_updates_unsequenced", row["decision_state"]["reasons"])
        self.assertEqual(len(row["decision_state"]["event_metadata"]), 2)
        self.assertEqual(row["counts"]["ambiguous_groups"], 1)
        self.assertEqual(row["scenarios"][1]["arrival_state"]["status"], "VALID")
        self.assertEqual(row["scenarios"][1]["status"], "DECISION_BLOCKED")

    def test_identical_full_raw_duplicates_deduplicate(self):
        quote = self.quote()
        row = self.analyze(self.fetch([quote, copy.deepcopy(quote)]))
        self.assertEqual(row["decision_state"]["status"], "VALID")
        self.assertEqual(row["counts"]["exact_duplicates"], 1)
        self.assertEqual(row["counts"]["unique_events"], 1)
        self.assertEqual(row["counts"]["ambiguous_groups"], 0)

    def test_same_price_timestamp_but_different_venue_is_not_a_duplicate(self):
        row = self.analyze(self.fetch([self.quote(), self.quote(bx="P")]))
        self.assertEqual(row["decision_state"]["status"], "INVALID")
        self.assertEqual(row["counts"]["exact_duplicates"], 0)

    def test_out_of_order_input_sorted_by_event_time(self):
        quotes = [self.quote(-200, 98, 100), self.quote(-100, 99, 101), self.quote(200, 100, 102)]
        ordered = self.analyze(self.fetch(quotes))
        reversed_ = self.analyze(self.fetch(list(reversed(quotes))))
        self.assertEqual(ordered["decision_state"], reversed_["decision_state"])
        self.assertEqual(ordered["scenarios"], reversed_["scenarios"])
        self.assertEqual(ordered["counts"], reversed_["counts"])

    def test_age_boundary_exact_max_allowed_and_one_nanosecond_more_stale(self):
        self.assertEqual(self.analyze(self.fetch([self.quote(-1000)]))["decision_state"]["status"], "VALID")
        quote = self.quote(t=(self.target - pd.Timedelta(seconds=1, nanoseconds=1)).isoformat())
        self.assertEqual(self.analyze(self.fetch([quote]))["decision_state"]["status"], "STALE")

    def test_stale_decision_cannot_recover_into_eligible_arrival(self):
        row = self.analyze(self.fetch([self.quote(-1100), self.quote(100)]))
        self.assertEqual(row["decision_state"]["status"], "STALE")
        self.assertEqual(row["scenarios"][1]["arrival_state"]["status"], "VALID")
        self.assertEqual(row["scenarios"][1]["status"], "DECISION_BLOCKED")

    def test_missing_prior_does_not_seed_from_exact_or_future_event(self):
        for quotes in ([], [self.quote(0)], [self.quote(100)]):
            row = self.analyze(self.fetch(quotes))
            self.assertEqual(row["decision_state"]["status"], "MISSING")
            self.assertTrue(all(item["status"] == "DECISION_BLOCKED" for item in row["scenarios"]))

    def test_outside_prefix_and_exclusive_end_quotes_do_not_seed(self):
        row = self.analyze(self.fetch([self.quote(-5001), self.quote(2000)]))
        self.assertEqual(row["decision_state"]["status"], "MISSING")
        self.assertEqual(row["counts"]["outside"], 2)
        self.assertIsNone(row["first_seed_at"])
        included = self.analyze(self.fetch([self.quote(-5000)]), max_age_ms=5000)
        self.assertEqual(included["decision_state"]["status"], "VALID")

    def test_locked_crossed_zero_nonfinite_boolean_sizes_invalidate(self):
        cases = [self.quote(-100, 99, 99), self.quote(-100, 102, 101), self.quote(-100, 0, 100),
                 self.quote(-100, float("nan"), 100), self.quote(-100, 99, float("inf")),
                 self.quote(-100, bs=0), self.quote(-100, **{"as": True})]
        for bad in cases:
            with self.subTest(bad=bad):
                row = self.analyze(self.fetch([self.quote(-200), bad]))
                self.assertEqual(row["decision_state"]["status"], "INVALID")
                json.dumps(row, allow_nan=False)

    def test_missing_malformed_unknown_or_multiple_stock_conditions_invalidate(self):
        cases = [None, "R", [], ["X"], ["R", "R"], ["R", "X"], [1], {"R": True}]
        for conditions in cases:
            bad = self.quote(c=conditions)
            row = self.analyze(self.fetch([self.quote(-200), bad]))
            self.assertEqual(row["decision_state"]["status"], "INVALID")
            self.assertIn("stock_condition_not_exact_single_regular_R", row["decision_state"]["reasons"])
        bad = self.quote()
        del bad["c"]
        self.assertEqual(self.analyze(self.fetch([bad]))["decision_state"]["status"], "INVALID")

    def test_unknown_stock_condition_is_preserved_for_diagnostics(self):
        row = self.analyze(self.fetch([self.quote(c=["X"], bx="P", ax="N", z="A")]))
        metadata = row["decision_state"]["event_metadata"][0]
        self.assertEqual(metadata["quote_conditions"], ["X"])
        self.assertEqual(metadata["bid_exchange"], "P")
        self.assertEqual(metadata["ask_exchange"], "N")
        self.assertEqual(metadata["tape"], "A")

    def test_missing_malformed_and_unknown_stock_tape_poison_latest_state(self):
        for tape in (None, "", "D", ["A"], 1):
            row = self.analyze(self.fetch([self.quote(-200), self.quote(z=tape)]))
            self.assertEqual(row["decision_state"]["status"], "INVALID")
            self.assertIn("stock_tape_missing_or_unknown", row["decision_state"]["reasons"])
        bad = self.quote()
        del bad["z"]
        self.assertEqual(self.analyze(self.fetch([bad]))["decision_state"]["status"], "INVALID")
        for tape in ("A", "B", "C"):
            self.assertEqual(self.analyze(self.fetch([self.quote(z=tape)]))["decision_state"]["status"], "VALID")

    def test_crypto_has_no_invented_stock_condition_requirement(self):
        quote = self.quote()
        del quote["c"]
        del quote["z"]
        fetch = self.fetch([quote], symbol="BTC/USD", feed="crypto_us")
        row = analyze_replay_window("BTC/USD", fetch, target=self.target)
        self.assertEqual(row["decision_state"]["status"], "VALID")
        self.assertEqual(row["decision_state"]["quote"]["condition_filter"], "stock_condition_filter_not_applied_to_crypto")

    def test_unparseable_timestamp_globally_rejects_instead_of_dropping_event(self):
        for bad in ({"t": "not-a-time"}, {}, None, self.quote(t="2026-09-04T13:35:00")):
            row = self.analyze(self.fetch([self.quote(), bad]))
            self.assertFalse(row["source_usable"])
            self.assertEqual(row["decision_state"]["status"], "SOURCE_REJECTED")
            self.assertEqual(row["counts"]["unparseable_timestamp"], 1)

    def test_wrong_or_missing_source_fields_and_incomplete_pages_reject_globally(self):
        mutations = {"symbol": [None, "JPM"], "kind": [None, "bars"], "feed": [None, "crypto_us", [], "unknown"],
                     "complete": [False, None, 1], "pages": [0, None, True, -1, 4], "records": [None, {}]}
        for field, values in mutations.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    fetch = self.fetch()
                    fetch[field] = value
                    row = self.analyze(fetch)
                    self.assertFalse(row["source_usable"])
                    self.assertEqual(row["decision_state"]["status"], "SOURCE_REJECTED")
                    self.assertTrue(all(item["buy_reference_price"] is None for item in row["scenarios"]))
        for field in ("symbol", "kind", "feed", "complete", "pages", "start", "end", "observed_at", "records"):
            fetch = self.fetch()
            del fetch[field]
            self.assertFalse(self.analyze(fetch)["source_usable"])

    def test_wrong_record_symbol_rejects_entire_window(self):
        row = self.analyze(self.fetch([self.quote(S="JPM")]))
        self.assertFalse(row["source_usable"])
        self.assertIn("record_declares_wrong_symbol", row["source_reasons"])

    def test_nonexact_coverage_and_premature_observation_reject(self):
        for field in ("start", "end"):
            for offset in (-1, 1):
                fetch = self.fetch()
                fetch[field] = (pd.Timestamp(fetch[field]) + pd.Timedelta(nanoseconds=offset)).isoformat()
                self.assertFalse(self.analyze(fetch)["source_usable"])
        self.assertFalse(self.analyze(self.fetch(observed_at=self.target.isoformat()))["source_usable"])

    def test_page_and_record_budget_bounded_without_processing_oversized_payload(self):
        self.assertTrue(self.analyze(self.fetch(pages=3))["source_usable"])
        row = self.analyze(self.fetch([self.quote()] * 30_001, pages=3))
        self.assertFalse(row["source_usable"])
        self.assertIn("record_count_exceeds_research_limit", row["source_reasons"])
        self.assertEqual(row["counts"]["received"], 30_001)
        self.assertEqual(row["counts"]["unprocessed_due_to_record_limit"], 30_001)
        self.assertEqual(row["decision_state"]["status"], "SOURCE_REJECTED")

    def test_timezone_spellings_normalize_for_duplicate_identity(self):
        quote = self.quote()
        equivalent = copy.deepcopy(quote)
        equivalent["t"] = pd.Timestamp(quote["t"]).tz_convert("America/New_York").isoformat()
        row = self.analyze(self.fetch([quote, equivalent]))
        self.assertEqual(row["counts"]["exact_duplicates"], 1)
        self.assertEqual(row["decision_state"]["status"], "VALID")

    def test_empty_or_malformed_fetch_is_json_safe_and_rejected(self):
        for fetch in ({}, None, [], "bad"):
            row = analyze_replay_window("SPY", fetch, target=self.target)
            self.assertFalse(row["source_usable"])
            self.assertEqual(row["decision_state"]["status"], "SOURCE_REJECTED")
            json.dumps(row, allow_nan=False)

    def test_invalid_parameters_fail_early(self):
        for symbol in ("AAPL", "BTC-USD", "ETH/USD", None):
            with self.assertRaises(ValueError):
                analyze_replay_window(symbol, self.fetch(), target=self.target)
        for age in (-1, 1.5, True):
            with self.assertRaises(ValueError):
                self.analyze(max_age_ms=age)
        for latencies in ((), (250, 0), (0, 0), (0, 2001), [0, 250], (True,)):
            with self.assertRaises(ValueError):
                self.analyze(latencies_ms=latencies)
        with self.assertRaises(ValueError):
            analyze_replay_window("SPY", self.fetch(), target=pd.Timestamp("2026-09-04T13:35:00"))


if __name__ == "__main__":
    unittest.main()
