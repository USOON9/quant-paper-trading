from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest

from quantpaper.marketdata.replay_protocol import build_replay_plan
from quantpaper.marketdata.study_protocol import build_study_plan


UTC = timezone.utc
STUDY_NOW = datetime(2026, 9, 7, 10, tzinfo=UTC)
REPLAY_NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)


class MarketDataReplayProtocolTests(unittest.TestCase):
    def setUp(self):
        self.study = build_study_plan(now=STUDY_NOW)

    def test_default_study_derives_exact_same_ninety_targets_and_feeds(self):
        replay = build_replay_plan(self.study, now=REPLAY_NOW)
        self.assertEqual(replay["contract"], "intraday_asof_replay_v1")
        self.assertEqual(replay["created_at"], "2026-09-07T12:00:00Z")
        self.assertEqual(replay["summary"], {
            "expected_windows": 90, "expected_request_segments": 90, "max_planned_pages": 270,
        })
        self.assertEqual(replay["source_study"]["sample_days_by_asset"],
                         self.study["summary"]["sample_days_by_asset"])
        self.assertEqual(len(replay["windows"]), 90)
        expected = [(row["symbol"], row["session"], name, row["targets"][name], row["feed"])
                    for row in self.study["observations"] for name in ("opening", "midday", "closing")]
        observed = [(row["symbol"], row["session"], row["window_name"], row["target"], row["feed"])
                    for row in replay["windows"]]
        self.assertEqual(observed, expected)
        self.assertEqual(len(observed), len(set(observed)))
        self.assertTrue(replay["research_only"])
        self.assertFalse(replay["execution_enabled"])

    def test_seven_second_windows_and_fixed_causal_policy(self):
        replay = build_replay_plan(self.study, now=REPLAY_NOW)
        first = replay["windows"][0]
        self.assertEqual(first, {
            "symbol": "SPY", "session": "2026-08-31", "window_name": "opening",
            "target": "2026-08-31T13:35:00Z", "feed": "sip",
            "start": "2026-08-31T13:34:55Z", "end": "2026-08-31T13:35:02Z",
        })
        for row in replay["windows"]:
            start, end, target = [datetime.fromisoformat(row[name].replace("Z", "+00:00"))
                                  for name in ("start", "end", "target")]
            self.assertEqual(target - start, timedelta(seconds=5))
            self.assertEqual(end - target, timedelta(seconds=2))
        policy = replay["policy"]
        self.assertEqual(policy["max_age_ms"], 1000)
        self.assertEqual(policy["latency_scenarios_ms"], [0, 250, 1000])
        self.assertEqual(policy["quote_time_rule"], "quote.t < asof")
        self.assertTrue(policy["equal_asof_excluded"])
        self.assertTrue(policy["invalid_updates_poison_state"])
        self.assertEqual(policy["stock_quote_conditions"], ["R"])
        self.assertEqual(policy["stock_tapes"], ["A", "B", "C"])
        self.assertFalse(policy["crypto_stock_conditions_applied"])
        self.assertFalse(policy["locked_quotes_allowed"])
        self.assertFalse(policy["crossed_quotes_allowed"])
        self.assertFalse(policy["nonpositive_prices_or_sizes_allowed"])

    def test_original_clock_not_current_latest_sessions_is_used(self):
        later = build_replay_plan(self.study, now=datetime(2026, 10, 1, tzinfo=UTC))
        same_day = build_replay_plan(self.study, now=REPLAY_NOW)
        self.assertEqual(later["windows"], same_day["windows"])
        self.assertEqual(later["source_study"], same_day["source_study"])

    def test_original_metadata_is_not_mutated_or_copied_into_replay_controls(self):
        self.study["collection"] = {"scope": "synthetic metadata", "unknown_policy": "ignored"}
        self.study["source_snapshot_sha256"] = "synthetic-source-reference"
        self.study["interpretation"] = ["narrative is not an executable protocol control"]
        before = deepcopy(self.study)
        replay = build_replay_plan(self.study, now=REPLAY_NOW)
        self.assertEqual(self.study, before)
        self.assertNotIn("collection", replay)
        self.assertNotIn("source_snapshot_sha256", replay)
        self.assertNotIn("unknown_policy", replay["policy"])

    def test_maximum_session_budget_is_180_windows_and_540_pages(self):
        maximum = build_study_plan(now=STUDY_NOW, sessions=10)
        replay = build_replay_plan(maximum, now=REPLAY_NOW)
        self.assertEqual(replay["summary"]["expected_windows"], 180)
        self.assertEqual(replay["summary"]["max_planned_pages"], 540)
        self.assertEqual(replay["limits"], {"max_windows": 180, "max_pages_per_fetch": 3, "max_pages": 540})
        for count in (0, 11, True, 5.0, "5"):
            bad = deepcopy(self.study)
            bad["sessions_per_asset"] = count
            with self.subTest(count=count), self.assertRaises(ValueError):
                build_replay_plan(bad, now=REPLAY_NOW)

    def test_subset_and_explicit_iex_source_remain_distinct_from_crypto(self):
        source = build_study_plan(now=STUDY_NOW, sessions=1, stock_feed="iex", symbols=("JNJ", "BTC/USD"))
        replay = build_replay_plan(source, now=REPLAY_NOW)
        self.assertEqual(replay["summary"]["expected_windows"], 6)
        self.assertEqual({row["feed"] for row in replay["windows"] if row["symbol"] == "JNJ"}, {"iex"})
        self.assertEqual({row["feed"] for row in replay["windows"] if row["symbol"] == "BTC/USD"}, {"crypto_us"})

    def test_early_close_and_dst_are_reconstructed_without_fixed_utc_assumptions(self):
        for original_now in (datetime(2026, 11, 28, 10, tzinfo=UTC), datetime(2026, 3, 10, 10, tzinfo=UTC)):
            with self.subTest(original_now=original_now):
                source = build_study_plan(now=original_now, sessions=2, symbols=("SPY",))
                replay = build_replay_plan(source, now=original_now + timedelta(hours=1))
                self.assertEqual([row["target"] for row in replay["windows"]],
                                 [row["targets"][name] for row in source["observations"]
                                  for name in ("opening", "midday", "closing")])

    def test_missing_core_fields_and_wrong_contract_rejected(self):
        for name in ("contract", "created_at", "cutoff", "research_only", "execution_enabled",
                     "sessions_per_asset", "symbols", "stock_feed", "crypto_feed", "observations",
                     "timing", "limits", "summary"):
            bad = deepcopy(self.study)
            bad.pop(name)
            with self.subTest(field=name), self.assertRaisesRegex(ValueError, "missing required"):
                build_replay_plan(bad, now=REPLAY_NOW)
        for value in ("intraday_quote_audit_v1", "intraday_asof_replay_v1", None, 1):
            bad = deepcopy(self.study)
            bad["contract"] = value
            with self.subTest(contract=value), self.assertRaisesRegex(ValueError, "contract"):
                build_replay_plan(bad, now=REPLAY_NOW)

    def test_duplicate_reordered_and_missing_observations_rejected(self):
        changes = (
            lambda plan: plan["observations"].append(deepcopy(plan["observations"][0])),
            lambda plan: plan["observations"].__setitem__(1, deepcopy(plan["observations"][0])),
            lambda plan: plan["observations"].reverse(),
            lambda plan: plan["observations"].pop(),
        )
        for change in changes:
            bad = deepcopy(self.study)
            change(bad)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "observations"):
                build_replay_plan(bad, now=REPLAY_NOW)

    def test_unplanned_symbols_feeds_targets_and_session_bounds_rejected(self):
        changes = (
            ("symbol", "AAPL"), ("feed", "iex"), ("session", "2026-09-07"),
            ("session_start", "2026-08-31T13:30:01Z"),
            ("session_end", "2026-08-31T21:00:00Z"),
            ("session_start", "2026-08-31T13:30:00"),
            ("session_start", "2026-08-31T14:30:00+01:00"),
        )
        for field, value in changes:
            bad = deepcopy(self.study)
            bad["observations"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, "observations"):
                build_replay_plan(bad, now=REPLAY_NOW)
        for target in ("2026-08-31T13:35:01Z", "2026-08-31T13:34:00Z", "2026-08-31T20:30:00Z"):
            bad = deepcopy(self.study)
            bad["observations"][0]["targets"]["opening"] = target
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "observations"):
                build_replay_plan(bad, now=REPLAY_NOW)

    def test_incorrect_budget_policy_and_boolean_numeric_substitution_rejected(self):
        changes = (
            ("timing", "quote_window_seconds", 7),
            ("timing", "latency_scenarios_ms", [0, 1000]),
            ("limits", "max_pages_per_fetch", 4),
            ("summary", "expected_observations", 29),
            ("summary", "expected_request_segments", 120.0),
        )
        for section, field, value in changes:
            bad = deepcopy(self.study)
            bad[section][field] = value
            with self.subTest(section=section, field=field), self.assertRaisesRegex(ValueError, section):
                build_replay_plan(bad, now=REPLAY_NOW)
        for field, value in (("research_only", 1), ("execution_enabled", 0), ("execution_enabled", True),
                             ("crypto_feed", "sip"), ("stock_feed", "boats"), ("symbols", ["SPY", "SPY"])):
            bad = deepcopy(self.study)
            bad[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                build_replay_plan(bad, now=REPLAY_NOW)

    def test_future_source_and_naive_or_non_utc_source_created_at_rejected(self):
        future = build_study_plan(now=datetime(2026, 9, 9, 10, tzinfo=UTC), sessions=1)
        with self.assertRaisesRegex(ValueError, "future"):
            build_replay_plan(future, now=REPLAY_NOW)
        with self.assertRaisesRegex(ValueError, "future"):
            build_replay_plan(self.study, now=STUDY_NOW - timedelta(microseconds=1))
        for created_at in ("2026-09-07T10:00:00", "2026-09-07T11:00:00+01:00", "bad-time", None):
            bad = deepcopy(self.study)
            bad["created_at"] = created_at
            with self.subTest(created_at=created_at), self.assertRaises(ValueError):
                build_replay_plan(bad, now=REPLAY_NOW)

    def test_completion_buffer_at_exact_source_time_is_respected(self):
        at_buffer = datetime(2026, 9, 4, 20, 30, tzinfo=UTC)
        source = build_study_plan(now=at_buffer, sessions=1, symbols=("SPY",))
        replay = build_replay_plan(source, now=at_buffer)
        self.assertEqual(replay["windows"][-1]["session"], "2026-09-04")
        with self.assertRaisesRegex(ValueError, "future"):
            build_replay_plan(source, now=at_buffer - timedelta(microseconds=1))

    def test_naive_clock_non_json_values_and_invalid_top_level_rejected(self):
        for now in (None, datetime(2026, 9, 7), "2026-09-07T12:00:00Z", 1):
            with self.subTest(now=now), self.assertRaises(ValueError):
                build_replay_plan(self.study, now=now)
        for source in (None, [], "study", 1):
            with self.subTest(source=source), self.assertRaises(ValueError):
                build_replay_plan(source, now=REPLAY_NOW)
        for value in (float("nan"), float("inf"), {1: "not a JSON object key"}, datetime(2026, 9, 7), (1, 2)):
            bad = deepcopy(self.study)
            bad["extra"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_replay_plan(bad, now=REPLAY_NOW)

    def test_json_safe_output_and_deterministic_timezone_normalization(self):
        local_now = datetime(2026, 9, 7, 13, tzinfo=timezone(timedelta(hours=1)))
        replay = build_replay_plan(self.study, now=local_now)
        self.assertEqual(json.loads(json.dumps(replay, allow_nan=False)), replay)
        self.assertEqual(replay, build_replay_plan(self.study, now=REPLAY_NOW))


if __name__ == "__main__":
    unittest.main()
