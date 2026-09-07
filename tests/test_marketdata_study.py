"""Offline collector integration: frozen evidence, failure circuits, isolation."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import Mock, patch

from quantpaper.marketdata import cli
from quantpaper.marketdata.client import MarketDataError
from quantpaper.marketdata.storage import read_report, write_json
from quantpaper.marketdata.study import capture_study, source_snapshot


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
GATES = {"ENABLE_ALPACA_PAPER": "NO", "ENABLE_ALPACA_PAPER_ROUND_TRIP": "NO"}
SYNTHETIC_SOURCE = {"files": {"synthetic.py": {
    "text": "# Synthetic source fixture\n",
    "sha256": hashlib.sha256(b"# Synthetic source fixture\n").hexdigest(),
}}, "python": "test", "libraries": {}, "scope": "offline test fixture"}


def digest_records(records):
    return hashlib.sha256(json.dumps(
        records, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


class StudyFakeClient:
    """A bounded in-memory data client; no credentials or broker state."""

    def __init__(self, *, before_fetch=None, error=None, page_capped=False):
        self.calls = []
        self.request_count = 0
        self.closed = 0
        self.before_fetch = before_fetch
        self.error = error
        self.page_capped = page_capped

    def fetch(self, kind, symbol, start, end, *, feed, max_pages):
        if self.before_fetch is not None:
            self.before_fetch(kind, symbol, start, end, feed, max_pages)
        self.calls.append((kind, symbol, start, end, feed, max_pages))
        self.request_count += 1
        if self.error is not None:
            raise self.error
        if kind == "bars":
            minutes = int((end - start).total_seconds() // 60)
            records = [{
                "t": (start + timedelta(minutes=index)).isoformat(),
                "o": 100., "h": 101., "l": 99., "c": 100., "v": 1000, "n": 10,
            } for index in range(minutes)]
        else:
            records = [{
                "t": (start + timedelta(milliseconds=delay)).isoformat(),
                "bp": 100. + index * .01, "ap": 100.02 + index * .01,
                "bs": 10, "as": 10, "bx": "N", "ax": "N",
            } for index, delay in enumerate((0, 250, 1000))]
        capped = self.page_capped and kind == "quotes"
        if capped:
            self.request_count += 2
        return {
            "symbol": symbol, "kind": kind, "feed": feed,
            "start": start.isoformat(), "end": end.isoformat(),
            "observed_at": NOW.isoformat(), "records": records,
            "pages": 3 if capped else 1, "complete": not capped,
            "truncation_reason": "max_pages_reached" if capped else None,
            "page_observed_at": [NOW.isoformat()] * (3 if capped else 1),
            "content_hash": digest_records(records),
        }

    def close(self):
        self.closed += 1


class MarketDataStudyCollectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve() / "project"
        self.project.mkdir()
        self.run = self.project / "artifacts/marketdata/study-001"
        self.env = self.project / ".env"
        self.env.write_text(
            "ENABLE_ALPACA_PAPER=NO\nENABLE_ALPACA_PAPER_ROUND_TRIP=NO\n"
            "APCA_API_KEY_ID=synthetic-study-key-sentinel\n"
            "APCA_API_SECRET_KEY=synthetic-study-secret-sentinel\n"
        )
        self.protected = [self.env]
        for relative in (
            "artifacts/yahoo_walkforward_v2_model.joblib", "artifacts/shadow-report.json",
            "artifacts/research-v3/frozen/manifest.json", "artifacts/research-v3/frozen/report.json",
            "artifacts/research-v3/frozen/predictions.csv", "artifacts/research-v3/frozen/completion.json",
            "data/research.duckdb", "automation-fixture.toml",
        ):
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"prior frozen evidence\n")
            self.protected.append(path)
        self.gate_patch = patch.dict(os.environ, GATES)
        self.gate_patch.start()
        self.addCleanup(self.gate_patch.stop)
        self.snapshot_patch = patch("quantpaper.marketdata.study.source_snapshot", return_value=SYNTHETIC_SOURCE)
        self.snapshot_patch.start()
        self.addCleanup(self.snapshot_patch.stop)

    def protected_hashes(self):
        return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.protected}

    def collect(self, client=None, *, symbols=("SPY", "BTC/USD"), run=None):
        with redirect_stderr(io.StringIO()):
            return capture_study(run or self.run, project_root=self.project, client=client,
                                 now=NOW, sessions=1, symbols=symbols)

    def raw_files(self, run=None):
        directory = run or self.run
        return [path for path in sorted(directory.glob("*.json"))
                if path.name not in {"source.json", "plan.json", "report.json", "completion.json"}]

    def test_full_offline_capture_freezes_plan_before_fetch_and_verifies_all_hashes(self):
        before = self.protected_hashes()
        frozen = {}

        def inspect_before_fetch(*args):
            self.assertTrue((self.run / "source.json").is_file())
            self.assertTrue((self.run / "plan.json").is_file())
            self.assertFalse((self.run / "completion.json").exists())
            source_raw = (self.run / "source.json").read_bytes()
            plan_raw = (self.run / "plan.json").read_bytes()
            plan = json.loads(plan_raw)
            self.assertEqual(plan["source_snapshot_sha256"], hashlib.sha256(source_raw).hexdigest())
            frozen.setdefault("source", source_raw)
            frozen.setdefault("plan", plan_raw)
            self.assertEqual(source_raw, frozen["source"])
            self.assertEqual(plan_raw, frozen["plan"])
            self.assertEqual(args[-1], 3)

        client = StudyFakeClient(before_fetch=inspect_before_fetch)
        report = self.collect(client)
        self.assertEqual(report["status"], "captured")
        self.assertEqual(report["counts"], {
            "planned_observations": 2, "planned_request_segments": 8,
            "attempted_request_segments": 8, "successful_request_segments": 8,
            "skipped_request_segments": 0, "http_attempts": 8, "retained_response_pages": 8,
            "raw_bars": 1830, "raw_quotes": 18,
        })
        self.assertEqual(report, read_report(self.run, self.project))
        self.assertEqual(len(client.calls), 8)
        self.assertEqual(client.closed, 0)
        self.assertEqual({call[4] for call in client.calls if call[1] == "SPY"}, {"sip"})
        self.assertEqual({call[4] for call in client.calls if call[1] == "BTC/USD"}, {"crypto_us"})
        self.assertTrue(report["source_unchanged_during_capture"])
        self.assertFalse(report["execution_enabled"])
        self.assertFalse(report["model_updated"])
        self.assertFalse(report["strategy_validated"])
        self.assertEqual(self.protected_hashes(), before)
        completion = json.loads((self.run / "completion.json").read_text())
        self.assertEqual(len(completion["artifacts"]), 11)
        self.assertEqual({path.name for path in self.run.iterdir()},
                         set(completion["artifacts"]) | {"completion.json"})
        for filename, expected in completion["artifacts"].items():
            path = self.run / filename
            self.assertFalse(path.is_symlink())
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)
            self.assertNotIn(b"synthetic-study-key-sentinel", path.read_bytes())
            self.assertNotIn(b"synthetic-study-secret-sentinel", path.read_bytes())
        for path in self.raw_files():
            raw = json.loads(path.read_text())
            self.assertEqual(raw["content_hash"], digest_records(raw["records"]))
        for row in report["observations"]:
            self.assertTrue(row["source_metadata_valid"])
            self.assertEqual(row["bar_quality"]["missing_count"], 0)
            for window in row["windows"].values():
                self.assertTrue(window["usable"])
                self.assertTrue(all(scenario["available"] for scenario in window["latency_scenarios"]))

    def test_create_only_directory_rejected_before_constructor_or_network(self):
        self.run.mkdir(parents=True)
        evidence = self.run / "existing.json"
        evidence.write_bytes(b"previous immutable run")
        with patch("quantpaper.marketdata.study.MarketDataClient.from_env") as constructor:
            with self.assertRaisesRegex(ValueError, "already exists"):
                self.collect()
            constructor.assert_not_called()
        self.assertEqual(evidence.read_bytes(), b"previous immutable run")
        self.assertEqual([path.name for path in self.run.iterdir()], ["existing.json"])

    def test_open_gate_blocks_before_credentials_or_fetch(self):
        client = StudyFakeClient()
        before = self.protected_hashes()
        for gate in GATES:
            with self.subTest(gate=gate), patch.dict(os.environ, {gate: "YES"}):
                with patch("quantpaper.marketdata.study.MarketDataClient.from_env") as constructor:
                    with self.assertRaisesRegex(RuntimeError, "closed order gates"):
                        self.collect()
                    constructor.assert_not_called()
                with self.assertRaises(RuntimeError):
                    self.collect(client)
        self.assertEqual(client.calls, [])
        self.assertEqual(client.closed, 0)
        self.assertFalse(self.run.exists())
        self.assertEqual(self.protected_hashes(), before)

    def test_open_file_gate_blocks_when_no_process_override(self):
        self.env.write_text("ENABLE_ALPACA_PAPER=YES\nENABLE_ALPACA_PAPER_ROUND_TRIP=NO\n")
        with patch.dict(os.environ, {}, clear=True):
            with patch("quantpaper.marketdata.study.MarketDataClient.from_env") as constructor:
                with self.assertRaises(RuntimeError):
                    self.collect()
                constructor.assert_not_called()
        self.assertFalse(self.run.exists())

    def test_gate_is_rechecked_before_next_segment_and_incomplete_run_has_no_completion(self):
        def open_gate(*args):
            os.environ["ENABLE_ALPACA_PAPER"] = "YES"

        client = StudyFakeClient(before_fetch=open_gate)
        with patch.dict(os.environ, GATES):
            with self.assertRaisesRegex(RuntimeError, "closed order gates"):
                self.collect(client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.closed, 0)
        self.assertEqual(len(self.raw_files()), 1)
        self.assertFalse((self.run / "report.json").exists())
        self.assertFalse((self.run / "completion.json").exists())
        with self.assertRaises(OSError):
            read_report(self.run, self.project)

    def test_http_errors_break_circuit_without_retry_or_fallback_and_keep_unrequested_rows(self):
        for category, status in (("authentication", 401), ("permission", 403), ("rate_limit", 429)):
            with self.subTest(category=category):
                run = self.run.parent / category
                client = StudyFakeClient(error=MarketDataError(category, "Synthetic safe failure", http_status=status))
                before = self.protected_hashes()
                report = self.collect(client, run=run)
                self.assertEqual(report["status"], "partial")
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(client.calls[0][4], "sip")
                self.assertEqual(client.closed, 0)
                self.assertEqual(report["counts"], {
                    "planned_observations": 2, "planned_request_segments": 8,
                    "attempted_request_segments": 1, "successful_request_segments": 0,
                    "skipped_request_segments": 7, "http_attempts": 1, "retained_response_pages": 0,
                    "raw_bars": 0, "raw_quotes": 0,
                })
                self.assertEqual(report["circuit_breaker"]["category"], category)
                self.assertEqual(len(report["errors"]), 1)
                raw = [json.loads(path.read_text()) for path in self.raw_files(run)]
                self.assertEqual(len(raw), 8)
                self.assertEqual(sum(item["attempted"] for item in raw), 1)
                self.assertTrue(all(item["observed_at"] is None for item in raw))
                self.assertTrue(all(item["failure_recorded_at"] for item in raw))
                self.assertTrue(all(item["records"] == [] and not item["complete"] for item in raw))
                self.assertTrue(all(item["content_hash"] == digest_records([]) for item in raw))
                self.assertEqual(sum(item["truncation_reason"] == "circuit_breaker_not_requested" for item in raw), 7)
                self.assertEqual(self.protected_hashes(), before)
                self.assertEqual(report, read_report(run, self.project))

    def test_unknown_client_error_also_stops_future_requests(self):
        client = StudyFakeClient(error=MarketDataError("unrecognized", "Synthetic safe failure"))
        report = self.collect(client, symbols=("SPY",))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(report["counts"]["skipped_request_segments"], 3)
        self.assertFalse(report["circuit_breaker"]["known_category"])
        self.assertEqual(report["status"], "partial")

    def test_page_cap_is_partial_and_retained_quotes_are_not_treated_as_complete(self):
        client = StudyFakeClient(page_capped=True)
        report = self.collect(client, symbols=("SPY",))
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["errors"], [])
        self.assertIsNone(report["circuit_breaker"])
        self.assertEqual(len(client.calls), 4)
        self.assertEqual({call[4] for call in client.calls}, {"sip"})
        self.assertEqual(report["counts"]["raw_quotes"], 9)
        self.assertEqual(report["counts"]["http_attempts"], 10)
        row = report["observations"][0]
        self.assertFalse(row["complete"])
        self.assertIsNone(row["opening_closing_spread_pair"])
        for window in row["windows"].values():
            self.assertFalse(window["usable"])
            self.assertFalse(window["distribution_usable"])
        for path in self.raw_files():
            raw = json.loads(path.read_text())
            if raw["kind"] == "quotes":
                self.assertEqual(len(raw["records"]), 3)
                self.assertEqual(raw["truncation_reason"], "max_pages_reached")

    def test_owned_client_is_paced_and_closed_on_success_and_exception(self):
        for error in (None, RuntimeError("synthetic unexpected fetch exception")):
            with self.subTest(error=error):
                client = StudyFakeClient(error=error)
                run = self.run.parent / ("owned-ok" if error is None else "owned-fail")
                with patch("quantpaper.marketdata.study.MarketDataClient.from_env", return_value=client) as constructor:
                    if error is None:
                        self.assertEqual(self.collect(run=run)["status"], "captured")
                    else:
                        with self.assertRaisesRegex(RuntimeError, "synthetic unexpected"):
                            self.collect(run=run)
                    constructor.assert_called_once_with(self.env, min_request_interval_seconds=.4)
                self.assertEqual(client.closed, 1)
                if error is not None:
                    self.assertFalse((run / "completion.json").exists())

    def test_injected_client_is_not_closed_on_unexpected_exception(self):
        client = StudyFakeClient(error=RuntimeError("synthetic unexpected fetch exception"))
        with self.assertRaisesRegex(RuntimeError, "synthetic unexpected"):
            self.collect(client)
        self.assertEqual(client.closed, 0)
        self.assertFalse((self.run / "completion.json").exists())

    def test_source_changes_are_flagged_without_rewriting_original_snapshot(self):
        changed = {**SYNTHETIC_SOURCE, "scope": "synthetic source changed during collection"}
        with patch("quantpaper.marketdata.study.source_snapshot", side_effect=[SYNTHETIC_SOURCE, changed]):
            report = self.collect(StudyFakeClient(), symbols=("SPY",))
        self.assertEqual(report["status"], "source_changed")
        self.assertFalse(report["source_unchanged_during_capture"])
        self.assertEqual(json.loads((self.run / "source.json").read_text()), SYNTHETIC_SOURCE)
        self.assertEqual(report, read_report(self.run, self.project))

    def test_completed_study_refuses_overwrite_and_detects_raw_tampering(self):
        self.collect(StudyFakeClient(), symbols=("SPY",))
        plan_path = self.run / "plan.json"
        original_plan = plan_path.read_bytes()
        with self.assertRaises(FileExistsError):
            write_json(plan_path, {"replacement": "must not replace frozen plan"})
        self.assertEqual(plan_path.read_bytes(), original_plan)
        raw_path = self.raw_files()[0]
        raw_path.write_bytes(b"{}\n")
        with self.assertRaisesRegex(ValueError, "integrity mismatch"):
            read_report(self.run, self.project)

    def test_study_show_validates_frozen_report_without_client_access(self):
        report = self.collect(StudyFakeClient(), symbols=("SPY",))
        output = io.StringIO()
        with patch.object(cli, "PROJECT_ROOT", self.project), redirect_stdout(output):
            with patch("quantpaper.marketdata.study.MarketDataClient.from_env") as constructor:
                result = cli.main(["show", "--run-dir", str(self.run)])
                constructor.assert_not_called()
        self.assertEqual(result, 0)
        shown = json.loads(output.getvalue())
        self.assertEqual(shown["contract"], "intraday_multi_session_v1")
        self.assertEqual(shown["counts"], report["counts"])
        self.assertFalse(shown["execution_enabled"])

    def test_legacy_show_still_works_without_constructing_a_client(self):
        self.run.mkdir(parents=True)
        legacy = {
            "contract": "intraday_quote_audit_v1", "status": "captured", "symbols": {},
            "access": {"scope": "synthetic historical access"}, "errors": [],
        }
        indexed = {"plan.json": write_json(self.run / "plan.json", {"contract": "intraday_quote_audit_v1"}),
                   "report.json": write_json(self.run / "report.json", legacy)}
        write_json(self.run / "completion.json", {"status": "complete", "artifacts": indexed})
        output = io.StringIO()
        with patch.object(cli, "PROJECT_ROOT", self.project), redirect_stdout(output):
            with patch("quantpaper.marketdata.cli.MarketDataClient.from_env") as constructor:
                self.assertEqual(cli.main(["show", "--run-dir", str(self.run)]), 0)
                constructor.assert_not_called()
        shown = json.loads(output.getvalue())
        self.assertEqual(shown["access"], legacy["access"])
        self.assertEqual(shown["symbols"], {})
        self.assertFalse(shown["execution_enabled"])

    def test_study_cli_rejects_date_overrides_without_collecting(self):
        for flag in ("--stock-session", "--crypto-session"):
            with self.subTest(flag=flag), redirect_stderr(io.StringIO()):
                with patch("quantpaper.marketdata.study.capture_study") as collect:
                    result = cli.main(["study", "--run-dir", str(self.run), flag, "2026-09-04"])
                    self.assertEqual(result, 2)
                    collect.assert_not_called()


class MarketDataStudySourceAndImportTests(unittest.TestCase):
    def test_source_snapshot_contains_only_explicit_source_and_matching_hashes(self):
        snapshot = source_snapshot()
        expected = {"main.py", "pyproject.toml", "src/quantpaper/scheduler.py", "src/quantpaper/sessions.py"}
        expected.update(f"src/quantpaper/marketdata/{name}.py" for name in (
            "__init__", "client", "cli", "storage", "windows", "quality",
            "study_protocol", "study_analytics", "study",
        ))
        self.assertEqual(set(snapshot["files"]), expected)
        for source in snapshot["files"].values():
            self.assertEqual(source["sha256"], hashlib.sha256(source["text"].encode()).hexdigest())

    def test_source_snapshot_rejects_direct_internal_and_external_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            for external in (False, True):
                with self.subTest(external=external):
                    project = base / ("external-project" if external else "internal-project")
                    project.mkdir()
                    target = (base if external else project) / "source-fixture.py"
                    target.write_text("# only a synthetic source fixture\n")
                    (project / "main.py").symlink_to(target)
                    with patch("quantpaper.marketdata.study.PROJECT_ROOT", project):
                        with self.assertRaisesRegex(ValueError, "cannot leave|symbolic links"):
                            source_snapshot()

    def test_source_snapshot_rejects_symlinked_source_ancestor(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            (project / "main.py").write_text("# synthetic main\n")
            (project / "pyproject.toml").write_text("# synthetic manifest\n")
            source_target = project / "actual-sources"
            (source_target / "quantpaper").mkdir(parents=True)
            (source_target / "quantpaper/scheduler.py").write_text("# synthetic scheduler\n")
            (project / "src").symlink_to(source_target, target_is_directory=True)
            with patch("quantpaper.marketdata.study.PROJECT_ROOT", project):
                with self.assertRaisesRegex(ValueError, "symbolic links"):
                    source_snapshot()

    def test_study_import_and_cli_help_do_not_load_trading_or_access_network(self):
        script = textwrap.dedent("""
            import importlib.abc
            import hashlib
            import json
            import os
            import socket
            import sys
            forbidden = ("alpaca.trading", "quantpaper.alpaca", "quantpaper.alpaca_cli",
                         "quantpaper.alpaca_paper", "quantpaper.shadow", "quantpaper.shadow_cli",
                         "quantpaper.warehouse", "duckdb", "sklearn", "joblib", "yfinance")
            class ForbidImports(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if any(fullname == name or fullname.startswith(name + ".") for name in forbidden):
                        raise AssertionError("forbidden study import: " + fullname)
                    return None
            def no_network(*args, **kwargs):
                raise AssertionError("study import/help attempted network")
            def env_hash():
                return hashlib.sha256(json.dumps(dict(os.environ), sort_keys=True).encode()).hexdigest()
            sys.meta_path.insert(0, ForbidImports())
            socket.create_connection = no_network
            socket.socket.connect = no_network
            before = env_hash()
            from quantpaper.marketdata.study import capture_study
            from quantpaper.marketdata.cli import main
            try:
                main(["--help"])
            except SystemExit as error:
                if error.code != 0:
                    raise
            else:
                raise AssertionError("help did not terminate normally")
            if env_hash() != before:
                raise AssertionError("study import/help mutated environment")
        """)
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["OPENBLAS_NUM_THREADS"] = "1"
        environment.setdefault("KMP_DUPLICATE_LIB_OK", "True")
        environment.setdefault("KMP_INIT_AT_FORK", "FALSE")
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-c", script], cwd=directory,
                                    env=environment, text=True, capture_output=True, timeout=40)
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("study", result.stdout)


if __name__ == "__main__":
    unittest.main()
