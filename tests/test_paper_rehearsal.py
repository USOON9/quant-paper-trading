"""Offline regression tests; any real SDK, credential or network path fails."""

from contextlib import ExitStack, contextmanager, redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from quantpaper import alpaca_paper, paper_rehearsal
from quantpaper.alpaca_cli import main


class PaperRehearsalTests(unittest.TestCase):
    @contextmanager
    def isolated(self):
        before_environment = dict(os.environ)
        original_pilot = alpaca_paper.PAPER_PILOT_DIRECTORY
        original_key_state = alpaca_paper.PAPER_STATE_DIRECTORY
        original_gate = alpaca_paper.AlpacaPaperService._require_gate
        with ExitStack() as stack:
            denied = [stack.enter_context(patch(target, side_effect=AssertionError("External access forbidden")))
                      for target in (
                          "quantpaper.alpaca_paper.AlpacaPaperService.__init__",
                          "quantpaper.alpaca_paper.TradingClient",
                          "quantpaper.alpaca_paper.StockHistoricalDataClient",
                          "quantpaper.alpaca_paper.PaperCredentials.load",
                          "quantpaper.alpaca_paper.load_dotenv",
                          "alpaca.trading.client.TradingClient.__init__",
                          "alpaca.data.historical.StockHistoricalDataClient.__init__",
                          "socket.socket.connect", "socket.socket.connect_ex",
                          "socket.create_connection", "socket.getaddrinfo",
                          "requests.sessions.Session.request", "urllib.request.urlopen",
                          "quantpaper.alpaca_paper.time.sleep",
                      )]
            yield
            self.assertFalse(any(mock.called for mock in denied), "An external or blocking path was used")
        # Compare as a boolean so a failure cannot print environment secrets.
        self.assertTrue(before_environment == dict(os.environ), "The environment was modified")
        self.assertIs(alpaca_paper.PAPER_PILOT_DIRECTORY, original_pilot)
        self.assertIs(alpaca_paper.PAPER_STATE_DIRECTORY, original_key_state)
        self.assertIs(alpaca_paper.AlpacaPaperService._require_gate, original_gate)

    def run_rehearsal(self):
        with self.isolated():
            return paper_rehearsal.rehearse()

    def test_all_scenarios_pass_without_external_access(self):
        result = self.run_rehearsal()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["mode"], "offline-synthetic-paper-rehearsal")
        self.assertEqual((result["scenarios_passed"], result["scenarios_total"]), (6, 6))
        self.assertEqual(result["synthetic_submit_calls"], 9)
        self.assertEqual(result["broker_requests"], 0)
        self.assertEqual(result["orders_submitted_to_alpaca"], 0)
        for key in ("model_used", "paper_trading_enabled", "execution_authorized"):
            self.assertIs(result[key], False)
        for key in ("environment_unchanged", "temporary_state_removed"):
            self.assertIs(result[key], True)
        self.assertEqual(tuple(row["scenario"] for row in result["scenarios"]),
                         paper_rehearsal.SCENARIO_NAMES)
        for row in result["scenarios"]:
            self.assertIs(row["passed"], True)
            self.assertEqual(row["expected"], row["observed"])

    def test_completed_and_partially_canceled_entries_exit_exact_quantity(self):
        rows = {row["scenario"]: row["observed"] for row in self.run_rehearsal()["scenarios"]}
        for name, quantity in (("whole_fill", "0.01"), ("partial_entry_canceled", "0.006487141")):
            with self.subTest(name=name):
                row = rows[name]
                self.assertEqual(row["state"], "COMPLETED_FLAT")
                self.assertEqual(row["confirmed_entry_quantity"], quantity)
                self.assertEqual(row["exit_requested_quantity"], quantity)
                self.assertEqual(row["remaining_known_quantity"], "0")
                self.assertFalse(row["key_marker_retained"])
                self.assertEqual(row["account_guard_reasons"], ["DAILY_LIMIT_REACHED"])

    def test_duplicate_and_new_key_same_account_cannot_repost(self):
        rows = {row["scenario"]: row["observed"] for row in self.run_rehearsal()["scenarios"]}
        row = rows["duplicate_and_same_account"]
        self.assertEqual(row["restart_states"], ["RUN_ID_REUSED", "DAILY_LIMIT_REACHED"])
        self.assertEqual(row["synthetic_submit_calls"], 2)
        self.assertEqual(row["additional_restart_submits"], 0)
        self.assertTrue(row["account_guard_blocked"])

    def test_stale_quote_never_reserves_guard_or_creates_entry_intent(self):
        rows = {row["scenario"]: row["observed"] for row in self.run_rehearsal()["scenarios"]}
        row = rows["stale_quote"]
        self.assertEqual(row["state"], "QUOTE_STALE_BLOCKED")
        self.assertEqual(row["synthetic_submit_calls"], 0)
        self.assertEqual(row["order_intents_recorded"], 0)
        self.assertFalse(row["key_marker_retained"])
        self.assertFalse(row["account_guard_blocked"])
        self.assertEqual(row["account_guard_reasons"], [])

    def test_ambiguous_submission_leaves_both_blocks_and_only_one_post(self):
        rows = {row["scenario"]: row["observed"] for row in self.run_rehearsal()["scenarios"]}
        row = rows["ambiguous_submission"]
        self.assertEqual(row["state"], "RECONCILIATION_REQUIRED")
        self.assertEqual(row["synthetic_submit_calls"], 1)
        self.assertEqual(row["synthetic_client_id_lookups"], 1)
        self.assertIsNone(row["confirmed_entry_quantity"])
        self.assertIsNone(row["remaining_known_quantity"])
        self.assertTrue(row["key_marker_retained"])
        self.assertIn("RECONCILIATION_REQUIRED", row["account_guard_reasons"])
        self.assertEqual(row["restart_states"], ["RECONCILIATION_REQUIRED"] * 2)
        self.assertEqual(row["additional_restart_submits"], 0)

    def test_residual_exit_blocks_restart_and_never_sends_extra_exit(self):
        rows = {row["scenario"]: row["observed"] for row in self.run_rehearsal()["scenarios"]}
        row = rows["residual_exit"]
        self.assertEqual(row["state"], "RECONCILIATION_REQUIRED")
        self.assertEqual(row["remaining_known_quantity"], "0.006")
        self.assertEqual(row["synthetic_exit_submits"], 1)
        self.assertTrue(row["key_marker_retained"])
        self.assertIn("RECONCILIATION_REQUIRED", row["account_guard_reasons"])
        self.assertEqual(row["restart_states"], ["RECONCILIATION_REQUIRED"] * 2)
        self.assertEqual(row["additional_restart_submits"], 0)

    def test_production_round_trip_is_called_for_all_runs(self):
        production = alpaca_paper.AlpacaPaperService.round_trip_stock
        calls = []

        def observe(service, *args, **kwargs):
            calls.append((type(service), args, kwargs))
            return production(service, *args, **kwargs)

        with self.isolated(), patch.object(alpaca_paper.AlpacaPaperService, "round_trip_stock", observe):
            paper_rehearsal.rehearse()
        self.assertEqual(len(calls), 12)
        self.assertTrue(all(service is alpaca_paper.AlpacaPaperService for service, _, _ in calls))

    def test_temp_state_resolved_isolated_and_removed(self):
        original_service = paper_rehearsal._service
        paths = []

        def observe(root, broker, **kwargs):
            service = original_service(root, broker, **kwargs)
            paths.extend((service.audit.path, service._lock_path, service._incident_path))
            self.assertEqual(root, root.resolve())
            self.assertTrue(alpaca_paper.PAPER_PILOT_DIRECTORY.is_relative_to(root.parent))
            return service

        with self.isolated(), patch.object(paper_rehearsal, "_service", observe):
            paper_rehearsal.rehearse()
        self.assertTrue(paths)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertTrue(all(not path.parent.exists() for path in paths))

    def test_unexpected_behavior_raises_and_restores_paths(self):
        created = []
        real_temp = TemporaryDirectory

        def temporary(*args, **kwargs):
            context = real_temp(*args, **kwargs)
            created.append(Path(context.name).resolve())
            return context

        with self.isolated(), patch.object(paper_rehearsal, "TemporaryDirectory", temporary):
            with patch.object(paper_rehearsal._SyntheticBroker, "get_clock",
                              side_effect=RuntimeError("Untrusted synthetic detail")):
                with self.assertRaisesRegex(RuntimeError, "^Synthetic Paper rehearsal failed unexpectedly$"):
                    paper_rehearsal.rehearse()
        self.assertTrue(created)
        self.assertTrue(all(not path.exists() for path in created))

    def test_assertions_are_not_optimizable_away(self):
        with self.assertRaisesRegex(RuntimeError, "^Synthetic Paper rehearsal assertion failed$"):
            paper_rehearsal._check(False)

    def test_json_has_no_identity_paths_or_execution_claims(self):
        result = self.run_rehearsal()
        encoded = json.dumps(result, sort_keys=True, allow_nan=False)
        self.assertEqual(json.loads(encoded), result)
        self.assertNotIn("account_id", encoded)
        self.assertNotIn("client_order_id", encoded)
        self.assertNotIn("/Users/", encoded)
        self.assertNotIn("/private/", encoded)
        self.assertNotIn(".jsonl", encoded)
        self.assertIn("not Alpaca acceptance", encoded)
        self.assertIn("not market observations", encoded)

    def test_cli_rehearse_avoids_credentials_even_with_unused_env_argument(self):
        output = io.StringIO()
        with self.isolated(), redirect_stdout(output):
            code = main(["rehearse", "--env", "must-not-be-read.env", "--audit", "must-not-be-written.jsonl"])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["orders_submitted_to_alpaca"], 0)


if __name__ == "__main__":
    unittest.main()
