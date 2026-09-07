"""Deterministic, offline synthetic-intent admission and lifecycle regressions."""

import ast
import copy
import inspect
import json
import unittest

import pandas as pd

from quantpaper import admission
from quantpaper.admission import deterministic_intent_id, evaluate_intent
from quantpaper.marketdata.replay_book import analyze_replay_window


class AdmissionTests(unittest.TestCase):
    target = pd.Timestamp("2026-09-04T13:35:00Z")

    def quote(self, milliseconds, **changes):
        return {"t": (self.target + pd.Timedelta(milliseconds=milliseconds)).isoformat(),
                "bp": 99, "ap": 101, "bs": 1, "as": 1, "c": ["R"], "z": "C",
                "bx": "V", "ax": "V", **changes}

    def replay(self, *, records=None, symbol="SPY", feed="sip"):
        fetch = {"symbol": symbol, "kind": "quotes", "feed": feed, "complete": True, "pages": 1,
                 "start": (self.target - pd.Timedelta(seconds=5)).isoformat(),
                 "end": (self.target + pd.Timedelta(seconds=2)).isoformat(),
                 "observed_at": "2026-09-07T12:00:00Z",
                 "records": [self.quote(-100), self.quote(100), self.quote(900)] if records is None else records}
        return analyze_replay_window(symbol, fetch, target=self.target)

    def intent(self, **changes):
        intent = {"symbol": "SPY", "side": "BUY", "notional_decimal": "5",
                  "decision_at": self.target.isoformat(), "latency_ms": 250,
                  "source_completion_sha256": "a" * 64, **changes}
        try:
            intent["id"] = deterministic_intent_id(intent)
        except ValueError:
            intent["id"] = "invalid-intent"
        return intent

    def policy(self, **changes):
        return {"paper_only": True, "execution_enabled": False, "max_notional_usd": "25",
                "max_quote_age_ms": 1000, "expected_source_completion_sha256": "a" * 64, **changes}

    def model(self, **changes):
        return {"verified": True, "approved_for_paper": True, "model_sha256": "b" * 64,
                "scope": "frozen_v2_gate_only_not_historical_prediction", **changes}

    def evaluate(self, intent=None, replay=None, model=None, policy=None):
        return evaluate_intent(intent if intent is not None else self.intent(),
                               replay if replay is not None else self.replay(),
                               model if model is not None else self.model(),
                               policy=policy if policy is not None else self.policy())

    def test_approved_means_simulation_only_with_three_terminal_logical_events(self):
        row = self.evaluate()
        self.assertEqual(row["state"], "APPROVED_FOR_SIMULATION_ONLY")
        self.assertEqual(row["reasons"], [])
        self.assertTrue(all(check["passed"] for check in row["checks"].values()))
        self.assertEqual([event["sequence"] for event in row["events"]], [1, 2, 3])
        self.assertEqual([event["event"] for event in row["events"]], ["INTENT_CREATED", "CHECKS_COMPLETED", "APPROVED_FOR_SIMULATION_ONLY"])
        self.assertTrue(row["events"][-1]["terminal"])
        self.assertFalse(row["submitted"])
        self.assertFalse(row["filled"])
        self.assertFalse(row["execution_enabled"])
        self.assertTrue(row["synthetic_intent"])
        self.assertIn("not model predictions", row["limitations"][0])
        json.dumps(row, allow_nan=False)

    def test_unapproved_model_does_not_hide_source_or_quote_failures(self):
        replay = self.replay(records=[self.quote(-200), self.quote(-100, bp=102, ap=101)])
        replay["source_usable"] = False
        row = self.evaluate(replay=replay, model=self.model(approved_for_paper=False))
        self.assertEqual(row["state"], "REJECTED")
        self.assertIn("MODEL_NOT_APPROVED", row["reasons"])
        self.assertIn("SOURCE_REJECTED", row["reasons"])
        self.assertIn("QUOTE_DECISION_INVALID", row["reasons"])
        self.assertIn("QUOTE_ARRIVAL_INVALID", row["reasons"])
        self.assertEqual(row["events"][-1]["event"], "REJECTED")

    def test_risk_limit_decimal_exact_boundary_and_stricter_policy(self):
        self.assertEqual(self.evaluate(intent=self.intent(notional_decimal="25.00000000"))["state"], "APPROVED_FOR_SIMULATION_ONLY")
        row = self.evaluate(intent=self.intent(notional_decimal="25.00000001"))
        self.assertIn("INTENT_NOTIONAL_LIMIT_EXCEEDED", row["reasons"])
        self.assertIn("INTENT_NOTIONAL_LIMIT_EXCEEDED", self.evaluate(policy=self.policy(max_notional_usd="4.99"))["reasons"])

    def test_notional_must_be_finite_positive_bounded_decimal_string(self):
        for value in (0, 5.0, True, "0", "-1", "NaN", "Infinity", "1e99", ".5", "", "0.000000001", "1" * 100):
            with self.subTest(value=value):
                row = self.evaluate(intent=self.intent(notional_decimal=value))
                self.assertIn("INTENT_NOTIONAL_INVALID", row["reasons"])
                self.assertEqual(row["state"], "REJECTED")
                json.dumps(row, allow_nan=False)

    def test_paper_or_execution_gate_cannot_be_open_or_missing(self):
        for changes in ({"paper_only": False}, {"execution_enabled": True}, {"paper_only": 1}, {"execution_enabled": 0}):
            self.assertEqual(self.evaluate(policy=self.policy(**changes))["state"], "REJECTED")
        self.assertEqual(self.evaluate(policy={})["state"], "REJECTED")

    def test_policy_cannot_raise_hard_notional_or_age_limits(self):
        for limit in ("100", "0", 25, "NaN"):
            self.assertIn("POLICY_NOTIONAL_LIMIT_INVALID", self.evaluate(policy=self.policy(max_notional_usd=limit))["reasons"])
        for age in (1001, -1, True, "1000"):
            self.assertIn("POLICY_QUOTE_AGE_LIMIT_INVALID", self.evaluate(policy=self.policy(max_quote_age_ms=age))["reasons"])
        self.assertIn("QUOTE_DECISION_STALE", self.evaluate(policy=self.policy(max_quote_age_ms=50))["reasons"])

    def test_model_verified_approval_scope_and_hash_are_independent_gates(self):
        cases = [({"verified": False}, "MODEL_EVIDENCE_UNVERIFIED"),
                 ({"approved_for_paper": False}, "MODEL_NOT_APPROVED"),
                 ({"approved_for_paper": 1}, "MODEL_NOT_APPROVED"),
                 ({"scope": "historical_prediction"}, "MODEL_EVIDENCE_SCOPE_INVALID"),
                 ({"model_sha256": "bad"}, "MODEL_EVIDENCE_HASH_INVALID"),
                 ({"model_sha256": None}, "MODEL_EVIDENCE_HASH_REQUIRED")]
        for changes, reason in cases:
            self.assertIn(reason, self.evaluate(model=self.model(**changes))["reasons"])

    def test_no_research_mode_can_bypass_unapproved_model(self):
        row = self.evaluate(model=self.model(approved_for_paper=False), policy=self.policy(research_mode=True, bypass_model_gate=True))
        self.assertIn("MODEL_NOT_APPROVED", row["reasons"])
        self.assertEqual(row["state"], "REJECTED")

    def test_intent_identity_is_canonical_deterministic_and_source_namespaced(self):
        first = self.intent()
        equivalent = self.intent(notional_decimal="05.000", decision_at=self.target.tz_convert("America/New_York").isoformat())
        self.assertEqual(first["id"], equivalent["id"])
        for changes in ({"side": "SELL"}, {"latency_ms": 1000}, {"notional_decimal": "6"},
                        {"symbol": "JPM"}, {"source_completion_sha256": "c" * 64}):
            self.assertNotEqual(first["id"], self.intent(**changes)["id"])
        self.assertEqual(self.evaluate(), self.evaluate())

    def test_bad_id_or_source_binding_rejects(self):
        intent = self.intent()
        intent["id"] = "a-different-id"
        self.assertIn("INTENT_ID_MISMATCH", self.evaluate(intent=intent)["reasons"])
        self.assertIn("SOURCE_COMPLETION_HASH_MISMATCH", self.evaluate(intent=self.intent(source_completion_sha256="c" * 64))["reasons"])
        self.assertIn("INTENT_SOURCE_HASH_INVALID", self.evaluate(intent=self.intent(source_completion_sha256="bad"))["reasons"])

    def test_symbol_side_latency_and_decision_must_match_supported_scope(self):
        for changes, reason in (({"symbol": "AAPL"}, "INTENT_SYMBOL_UNSUPPORTED"),
                                ({"side": "SHORT"}, "INTENT_SIDE_INVALID"),
                                ({"latency_ms": True}, "INTENT_LATENCY_INVALID"),
                                ({"latency_ms": 500}, "INTENT_LATENCY_INVALID"),
                                ({"decision_at": "2026-09-04T13:35:00"}, "INTENT_DECISION_TIME_INVALID")):
            self.assertIn(reason, self.evaluate(intent=self.intent(**changes))["reasons"])
        self.assertIn("SOURCE_SYMBOL_MISMATCH", self.evaluate(intent=self.intent(symbol="JPM"))["reasons"])
        shifted = (self.target + pd.Timedelta(seconds=1)).isoformat()
        self.assertIn("SOURCE_TARGET_TIME_MISMATCH", self.evaluate(intent=self.intent(decision_at=shifted))["reasons"])

    def test_source_metadata_symbol_feed_pages_and_observation_fail_closed(self):
        mutations = [({"symbol": "JPM"}, "SOURCE_SYMBOL_MISMATCH"), ({"feed": "iex"}, "SOURCE_FEED_MISMATCH"),
                     ({"kind": "bars"}, "SOURCE_METADATA_INVALID"), ({"complete": False}, "SOURCE_METADATA_INVALID"),
                     ({"pages": 4}, "SOURCE_PAGE_COUNT_INVALID"), ({"observed_at": self.target.isoformat()}, "SOURCE_OBSERVATION_TIME_INVALID")]
        for changes, reason in mutations:
            replay = self.replay()
            replay["source"].update(changes)
            self.assertIn(reason, self.evaluate(replay=replay)["reasons"])

    def test_source_claims_cannot_override_event_integrity_counts_or_range(self):
        replay = self.replay()
        replay["counts"]["unparseable_timestamp"] = 1
        self.assertIn("SOURCE_EVENT_INTEGRITY_INVALID", self.evaluate(replay=replay)["reasons"])
        replay = self.replay()
        replay["source"]["start"] = (self.target - pd.Timedelta(seconds=6)).isoformat()
        self.assertIn("SOURCE_WINDOW_COVERAGE_INVALID", self.evaluate(replay=replay)["reasons"])
        replay = self.replay()
        replay["policy"]["strictly_before_asof"] = False
        self.assertIn("SOURCE_CAUSAL_POLICY_INVALID", self.evaluate(replay=replay)["reasons"])

    def test_replay_live_or_fill_flags_never_allow_execution(self):
        for field, value in (("research_only", False), ("execution_enabled", True), ("fills_assumed", True)):
            replay = self.replay()
            replay[field] = value
            row = self.evaluate(replay=replay)
            self.assertIn("REPLAY_RESEARCH_ONLY_FLAGS_REQUIRED", row["reasons"])
            self.assertFalse(row["execution_enabled"])
            self.assertFalse(row["submitted"])

    def test_spoofed_valid_future_quote_rejected_even_with_age_zero(self):
        replay = self.replay()
        state = replay["decision_state"]
        state["event_at"] = self.target.isoformat()
        state["quote"]["timestamp"] = self.target.isoformat()
        state["age_ms"] = 0
        self.assertIn("QUOTE_DECISION_EVENT_NOT_STRICTLY_PRIOR", self.evaluate(replay=replay)["reasons"])

    def test_spoofed_valid_stale_quote_recomputes_age_instead_of_trusting_field(self):
        replay = self.replay()
        state = replay["decision_state"]
        at = (self.target - pd.Timedelta(milliseconds=1001)).isoformat()
        state["event_at"] = at
        state["quote"]["timestamp"] = at
        state["age_ms"] = 0
        row = self.evaluate(replay=replay)
        self.assertIn("QUOTE_DECISION_STALE", row["reasons"])
        self.assertIn("QUOTE_DECISION_AGE_INCONSISTENT", row["reasons"])

    def test_age_boundary_1000_ms_passes_exactly_and_next_nanosecond_fails(self):
        replay = self.replay(records=[self.quote(-1000), self.quote(100)])
        self.assertEqual(self.evaluate(replay=replay)["state"], "APPROVED_FOR_SIMULATION_ONLY")
        quote = self.quote(-1000)
        quote["t"] = (self.target - pd.Timedelta(seconds=1, nanoseconds=1)).isoformat()
        self.assertIn("QUOTE_DECISION_STALE", self.evaluate(replay=self.replay(records=[quote, self.quote(100)]))["reasons"])

    def test_quote_payload_crossed_nan_boolean_and_condition_spoofs_rejected(self):
        cases = [({"bid_price": 102}, "LOCKED_OR_CROSSED"), ({"ask_price": float("nan")}, "PRICE_OR_SIZE_INVALID"),
                 ({"bid_size_raw_units": True}, "PRICE_OR_SIZE_INVALID"), ({"quote_conditions": ["X"]}, "CONDITION_INVALID"),
                 ({"tape": "D"}, "TAPE_INVALID"), ({"condition_filter": "accept_all"}, "CONDITION_POLICY_INVALID")]
        for changes, suffix in cases:
            replay = self.replay()
            replay["decision_state"]["quote"].update(changes)
            row = self.evaluate(replay=replay)
            self.assertIn("QUOTE_DECISION_" + suffix, row["reasons"])
            json.dumps(row, allow_nan=False)

    def test_unsequenced_metadata_cannot_be_spoofed_valid(self):
        replay = self.replay()
        replay["decision_state"]["event_metadata"].append(copy.deepcopy(replay["decision_state"]["event_metadata"][0]))
        self.assertIn("QUOTE_DECISION_EVENT_METADATA_AMBIGUOUS_OR_MISSING", self.evaluate(replay=replay)["reasons"])

    def test_blocked_decision_remains_rejected_after_arrival_recovery(self):
        replay = self.replay(records=[self.quote(-100, bp=102), self.quote(100)])
        row = self.evaluate(replay=replay)
        self.assertIn("QUOTE_DECISION_INVALID", row["reasons"])
        self.assertTrue(row["checks"]["arrival_quote"]["passed"])
        self.assertEqual(row["state"], "REJECTED")

    def test_missing_prior_quote_rejected_despite_future_seed(self):
        row = self.evaluate(replay=self.replay(records=[self.quote(100)]))
        self.assertIn("QUOTE_DECISION_MISSING", row["reasons"])
        self.assertEqual(row["state"], "REJECTED")

    def test_arrival_event_and_scenario_time_checked_causally(self):
        replay = self.replay()
        state = replay["scenarios"][1]["arrival_state"]
        at = (self.target + pd.Timedelta(milliseconds=250)).isoformat()
        state["event_at"] = at
        state["quote"]["timestamp"] = at
        state["age_ms"] = 0
        self.assertIn("QUOTE_ARRIVAL_EVENT_NOT_STRICTLY_PRIOR", self.evaluate(replay=replay)["reasons"])
        replay = self.replay()
        replay["scenarios"][1]["arrival_at"] = self.target.isoformat()
        self.assertIn("QUOTE_ARRIVAL_TIME_MISMATCH", self.evaluate(replay=replay)["reasons"])

    def test_missing_duplicate_or_spoofed_scenario_cannot_admit(self):
        replay = self.replay()
        replay["scenarios"].pop(1)
        self.assertIn("QUOTE_ARRIVAL_SCENARIO_MISSING_OR_AMBIGUOUS", self.evaluate(replay=replay)["reasons"])
        replay = self.replay()
        replay["scenarios"].append(copy.deepcopy(replay["scenarios"][1]))
        self.assertIn("QUOTE_ARRIVAL_SCENARIO_MISSING_OR_AMBIGUOUS", self.evaluate(replay=replay)["reasons"])
        replay = self.replay()
        replay["scenarios"][1]["buy_reference_price"] = 1
        self.assertIn("QUOTE_ARRIVAL_REFERENCE_PRICE_INCONSISTENT", self.evaluate(replay=replay)["reasons"])

    def test_zero_latency_requires_exact_decision_state(self):
        row = self.evaluate(intent=self.intent(latency_ms=0))
        self.assertEqual(row["state"], "APPROVED_FOR_SIMULATION_ONLY")
        replay = self.replay()
        replay["scenarios"][0]["arrival_state"] = copy.deepcopy(replay["scenarios"][0]["arrival_state"])
        replay["scenarios"][0]["arrival_state"]["quote"]["bid_exchange"] = "P"
        self.assertIn("QUOTE_ARRIVAL_ZERO_LATENCY_STATE_MISMATCH", self.evaluate(intent=self.intent(latency_ms=0), replay=replay)["reasons"])

    def test_arrival_event_cannot_regress_behind_valid_decision_event(self):
        replay = self.replay()
        state = replay["scenarios"][1]["arrival_state"]
        earlier = (self.target - pd.Timedelta(milliseconds=200)).isoformat()
        state["event_at"] = earlier
        state["quote"]["timestamp"] = earlier
        state["age_ms"] = 450
        row = self.evaluate(replay=replay)
        self.assertEqual(row["state"], "REJECTED")
        self.assertIn("QUOTE_ARRIVAL_EVENT_REGRESSION", row["reasons"])
        self.assertTrue(row["checks"]["decision_quote"]["passed"])

    def test_same_event_cannot_change_quote_or_metadata_at_later_arrival(self):
        for mutation in ("price", "exchange"):
            with self.subTest(mutation=mutation):
                replay = self.replay()
                scenario = replay["scenarios"][1]
                decision = replay["decision_state"]
                state = scenario["arrival_state"]
                state["event_at"] = decision["event_at"]
                state["age_ms"] = 350
                state["quote"] = copy.deepcopy(decision["quote"])
                state["event_metadata"] = copy.deepcopy(decision["event_metadata"])
                if mutation == "price":
                    state["quote"]["ask_price"] = 102
                    scenario["buy_reference_price"] = 102
                else:
                    state["quote"]["ask_exchange"] = "P"
                    state["event_metadata"][0]["ask_exchange"] = "P"
                row = self.evaluate(replay=replay)
                self.assertEqual(row["state"], "REJECTED")
                self.assertIn("QUOTE_ARRIVAL_SAME_EVENT_CONTENT_MISMATCH", row["reasons"])

    def test_same_unchanged_event_remains_valid_with_different_asof_and_age(self):
        replay = self.replay(records=[self.quote(-100)])
        row = self.evaluate(replay=replay)
        self.assertEqual(row["state"], "APPROVED_FOR_SIMULATION_ONLY")
        self.assertEqual(replay["decision_state"]["age_ms"], 100)
        self.assertEqual(replay["scenarios"][1]["arrival_state"]["age_ms"], 350)

    def test_crypto_separate_feed_policy_does_not_require_stock_conditions(self):
        quotes = [self.quote(-100), self.quote(100)]
        for quote in quotes:
            del quote["c"]
            del quote["z"]
        row = self.evaluate(intent=self.intent(symbol="BTC/USD", side="SELL"),
                            replay=self.replay(records=quotes, symbol="BTC/USD", feed="crypto_us"))
        self.assertEqual(row["state"], "APPROVED_FOR_SIMULATION_ONLY")
        self.assertFalse(row["submitted"])

    def test_explicit_iex_policy_required_and_no_silent_feed_fallback(self):
        replay = self.replay(feed="iex")
        self.assertIn("SOURCE_FEED_MISMATCH", self.evaluate(replay=replay)["reasons"])
        self.assertEqual(self.evaluate(replay=replay, policy=self.policy(expected_stock_feed="iex"))["state"], "APPROVED_FOR_SIMULATION_ONLY")

    def test_malformed_inputs_json_safe_accumulated_rejection_not_exception(self):
        for value in (None, {}, [], "bad"):
            row = evaluate_intent(value, value, value, policy=value)
            self.assertEqual(row["state"], "REJECTED")
            self.assertIn("MODEL_NOT_APPROVED", row["reasons"])
            json.dumps(row, allow_nan=False)

    def test_kernel_has_no_network_broker_pickle_or_persistence_imports(self):
        tree = ast.parse(inspect.getsource(admission))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        forbidden = ("requests", "http", "alpaca", "socket", "pickle", "joblib", "duckdb", "sqlite", "pathlib", "os")
        self.assertFalse(any(name.startswith(forbidden) for name in imports))


if __name__ == "__main__":
    unittest.main()
