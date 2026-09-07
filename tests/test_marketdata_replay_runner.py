"""Quote pre-roll collection and offline replay cannot touch trading state."""

from __future__ import annotations

from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from quantpaper.marketdata.client import MarketDataError
from quantpaper.marketdata.replay_runner import run_replay
from quantpaper.marketdata.storage import read_report, write_json
from quantpaper.marketdata.study_protocol import build_study_plan


NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
GATES = {"ENABLE_ALPACA_PAPER": "NO", "ENABLE_ALPACA_PAPER_ROUND_TRIP": "NO"}
SNAPSHOT = {"files": {"synthetic.py": {"sha256": "test-only", "text": "# test fixture"}}}


def content_hash(records):
    return hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


class FakeQuotesClient:
    def __init__(self, *, complete=True, error=None, after_fetch=None):
        self.calls = []
        self.request_count = 0
        self.complete = complete
        self.error = error
        self.after_fetch = after_fetch
        self.closed = False

    def fetch(self, kind, symbol, start, end, *, feed, max_pages):
        self.calls.append((kind, symbol, start, end, feed, max_pages))
        self.request_count += 1
        if self.error:
            raise self.error
        target = start + timedelta(seconds=5)
        records = []
        for offset in (-100, 200, 900):
            record = {"t": (target + timedelta(milliseconds=offset)).isoformat(),
                      "bp": 100, "ap": 100.01, "bs": 10, "as": 12}
            if symbol != "BTC/USD":
                record.update(c=["R"], z="B", bx="N", ax="N")
            records.append(record)
        if self.after_fetch:
            self.after_fetch(self)
        return {"symbol": symbol, "kind": kind, "feed": feed,
                "start": start.isoformat(), "end": end.isoformat(),
                "observed_at": NOW.isoformat(), "records": records,
                "pages": 1 if self.complete else 3, "complete": self.complete,
                "truncation_reason": None if self.complete else "page_limit",
                "content_hash": content_hash(records)}

    def close(self):
        self.closed = True


class ReplayRunnerIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name).resolve() / "project"
        self.project.mkdir()
        self.base = self.project / "artifacts/marketdata"
        self.base.mkdir(parents=True)
        self.source = self.base / "frozen-study"
        self.source.mkdir()
        self.destination = self.base / "new-replay"
        self.study_plan = build_study_plan(now=NOW, sessions=1, symbols=("SPY", "BTC/USD"))
        snapshot_hash = write_json(self.source / "source.json", SNAPSHOT)
        self.study_plan["source_snapshot_sha256"] = snapshot_hash
        plan_hash = write_json(self.source / "plan.json", self.study_plan)
        report_hash = write_json(self.source / "report.json", {
            "contract": "intraday_multi_session_v1", "status": "captured",
            "source_unchanged_during_capture": True,
            "plan_sha256": plan_hash, "source_snapshot_sha256": snapshot_hash,
        })
        self.source_index = {"source.json": snapshot_hash, "plan.json": plan_hash, "report.json": report_hash}
        write_json(self.source / "completion.json", {"status": "complete", "artifacts": self.source_index})
        self.env_path = self.project / ".env"
        self.env_path.write_text("ENABLE_ALPACA_PAPER=NO\nENABLE_ALPACA_PAPER_ROUND_TRIP=NO\n"
                                 "APCA_API_KEY_ID=synthetic-key-never-output\n"
                                 "APCA_API_SECRET_KEY=synthetic-secret-never-output\n")
        self.protected = [self.env_path, *self.source.iterdir()]
        for name in ("artifacts/yahoo_walkforward_v2_model.joblib",
                     "artifacts/yahoo_walkforward_v2_model.json",
                     "artifacts/research-v3/frozen/completion.json",
                     "data/research.duckdb", "data/shadow/SPY.csv", "automation-fixture.toml"):
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"previous-stage immutable evidence\n")
            self.protected.append(path)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, GATES).start()
        self.snapshot = patch("quantpaper.marketdata.replay_runner.source_snapshot", return_value=SNAPSHOT).start()

    def hashes(self):
        return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.protected}

    def run_capture(self, client=None, destination=None, source=None):
        with redirect_stderr(io.StringIO()):
            return run_replay(destination or self.destination, source or self.source, collect=True,
                              project_root=self.project, client=client, now=NOW)

    def test_capture_preserves_protected_files_and_records_only_quote_windows(self):
        before = self.hashes()
        client = FakeQuotesClient()
        report = self.run_capture(client)
        self.assertEqual(report["status"], "captured")
        self.assertEqual(self.hashes(), before)
        self.assertFalse(client.closed, "caller owns an injected client")
        self.assertEqual(len(client.calls), 6)
        self.assertEqual(report["counts"]["http_attempts"], 6)
        self.assertEqual(report["counts"]["raw_quotes"], 18)
        for kind, symbol, start, end, feed, pages in client.calls:
            self.assertEqual(kind, "quotes")
            self.assertEqual((end - start).total_seconds(), 7)
            self.assertEqual(pages, 3)
            self.assertEqual(feed, "crypto_us" if symbol == "BTC/USD" else "sip")
        self.assertEqual(report["aggregate"]["decision_states"], {"VALID": 6})
        self.assertEqual(report["aggregate"]["expected_scenarios"], 18)
        self.assertEqual(report["fills_simulated"], 0)
        self.assertEqual(report["orders_submitted"], 0)
        self.assertFalse(report["execution_enabled"])
        self.assertFalse(report["model_updated"])
        self.assertEqual(read_report(self.destination, self.project), report)
        visible = b"".join(path.read_bytes() for path in self.destination.iterdir())
        self.assertNotIn(b"synthetic-key-never-output", visible)
        self.assertNotIn(b"synthetic-secret-never-output", visible)

    def test_plan_and_snapshot_are_indexed_before_first_fetch(self):
        def verify_plan(client):
            plan = json.loads((self.destination / "plan.json").read_text())
            for filename, key in (("source-plan.json", "source_study_plan_sha256"),
                                  ("source.json", "source_snapshot_sha256")):
                self.assertEqual(hashlib.sha256((self.destination / filename).read_bytes()).hexdigest(), plan[key])
            self.assertEqual(plan["input_evidence"]["plan_sha256"], self.source_index["plan.json"])
        self.run_capture(FakeQuotesClient(after_fetch=verify_plan))

    def test_owned_client_is_paced_and_closed(self):
        client = FakeQuotesClient()
        with patch("quantpaper.marketdata.replay_runner.MarketDataClient.from_env", return_value=client) as constructor:
            self.run_capture()
        constructor.assert_called_once_with(self.env_path, min_request_interval_seconds=0.4)
        self.assertTrue(client.closed)

    def test_owned_client_closes_even_when_gate_opens_between_segments(self):
        def open_gate(client):
            os.environ["ENABLE_ALPACA_PAPER"] = "YES"
        client = FakeQuotesClient(after_fetch=open_gate)
        with patch("quantpaper.marketdata.replay_runner.MarketDataClient.from_env", return_value=client):
            with self.assertRaises(RuntimeError):
                self.run_capture()
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(client.closed)
        self.assertFalse((self.destination / "completion.json").exists())

    def test_both_environment_gates_block_before_credentials_or_fetch(self):
        for gate in GATES:
            with self.subTest(gate=gate), patch.dict(os.environ, {gate: "YES"}):
                with patch("quantpaper.marketdata.replay_runner.MarketDataClient.from_env") as constructor:
                    with self.assertRaises(RuntimeError):
                        self.run_capture()
                    constructor.assert_not_called()
                client = Mock()
                with self.assertRaises(RuntimeError):
                    self.run_capture(client)
                client.fetch.assert_not_called()
                self.assertFalse(self.destination.exists())

    def test_open_file_gate_blocks_with_environment_unset(self):
        self.env_path.write_text("ENABLE_ALPACA_PAPER=NO\nENABLE_ALPACA_PAPER_ROUND_TRIP=YES\n")
        with patch.dict(os.environ, {}, clear=True):
            with patch("quantpaper.marketdata.replay_runner.MarketDataClient.from_env") as constructor:
                with self.assertRaises(RuntimeError):
                    self.run_capture()
                constructor.assert_not_called()

    def test_offline_replays_identical_rows_without_any_client(self):
        captured = self.run_capture(FakeQuotesClient())
        frozen_before = {path.name: path.read_bytes() for path in self.destination.iterdir()}
        with patch("quantpaper.marketdata.replay_runner.MarketDataClient.from_env", side_effect=AssertionError("credential access")):
            with patch("quantpaper.marketdata.client.requests.Session", side_effect=AssertionError("network access")):
                with redirect_stderr(io.StringIO()):
                    replayed = run_replay(self.base / "offline-copy", self.destination,
                                          project_root=self.project, now=NOW)
        self.assertEqual(replayed["status"], "replayed")
        self.assertEqual(replayed["windows"], captured["windows"])
        self.assertEqual(replayed["aggregate"], captured["aggregate"])
        self.assertEqual(replayed["counts"]["attempted_request_segments"], 0)
        self.assertEqual(replayed["counts"]["http_attempts"], 0)
        self.assertEqual({path.name: path.read_bytes() for path in self.destination.iterdir()}, frozen_before)
        with self.assertRaises(ValueError):
            run_replay(self.base / "offline-injected", self.destination, client=Mock(),
                       project_root=self.project, now=NOW)

    def test_tampered_source_plan_is_rejected_before_client_construction(self):
        (self.source / "plan.json").write_text("{}")
        with patch("quantpaper.marketdata.replay_runner.MarketDataClient.from_env") as constructor:
            with self.assertRaises(ValueError):
                self.run_capture()
            constructor.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_reindexed_but_unpaired_source_report_is_rejected(self):
        report_path = self.source / "report.json"
        original = json.loads(report_path.read_text())
        for field, value in (("plan_sha256", "0" * 64),
                             ("source_snapshot_sha256", "0" * 64),
                             ("source_unchanged_during_capture", "true")):
            with self.subTest(field=field):
                report_path.write_text(json.dumps({**original, field: value}))
                index = {**self.source_index, "report.json": hashlib.sha256(report_path.read_bytes()).hexdigest()}
                (self.source / "completion.json").write_text(json.dumps({"status": "complete", "artifacts": index}))
                client = Mock()
                with self.assertRaises(ValueError):
                    self.run_capture(client)
                client.fetch.assert_not_called()
                self.assertFalse(self.destination.exists())

    def test_payload_feed_must_match_frozen_plan_and_never_fall_back(self):
        class WrongFeed(FakeQuotesClient):
            def fetch(self, *args, **kwargs):
                payload = super().fetch(*args, **kwargs)
                payload["feed"] = "iex"
                return payload
        client = WrongFeed()
        with self.assertRaises(ValueError):
            self.run_capture(client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][4], "sip")
        self.assertFalse((self.destination / "completion.json").exists())

    def test_content_hash_mismatch_cannot_produce_complete_artifact(self):
        class BadContentHash(FakeQuotesClient):
            def fetch(self, *args, **kwargs):
                payload = super().fetch(*args, **kwargs)
                payload["content_hash"] = "0" * 64
                return payload
        with self.assertRaises(ValueError):
            self.run_capture(BadContentHash())
        self.assertFalse((self.destination / "completion.json").exists())

    def test_tampered_or_unindexed_raw_cannot_be_offline_replayed(self):
        self.run_capture(FakeQuotesClient())
        raw = next(self.destination.glob("*-quotes.json"))
        original = raw.read_bytes()
        raw.write_bytes(original + b" ")
        with self.assertRaises(ValueError):
            run_replay(self.base / "tampered-copy", self.destination, project_root=self.project, now=NOW)
        raw.write_bytes(original)
        index_path = self.destination / "completion.json"
        index = json.loads(index_path.read_text())
        del index["artifacts"][raw.name]
        index_path.write_text(json.dumps(index))
        with self.assertRaises(ValueError):
            run_replay(self.base / "unindexed-copy", self.destination, project_root=self.project, now=NOW)

    def test_existing_output_roots_and_symlinks_are_rejected_without_fetch(self):
        self.destination.mkdir()
        sentinel = self.destination / "existing.txt"
        sentinel.write_text("keep")
        output_link = self.base / "output-alias"
        output_link.symlink_to(self.base, target_is_directory=True)
        source_link = self.base / "source-alias"
        source_link.symlink_to(self.source, target_is_directory=True)
        client = Mock()
        for output, source in ((self.destination, self.source), (self.project, self.source),
                               (self.base, self.source), (output_link / "child", self.source),
                               (self.base / "source-alias-copy", source_link)):
            with self.subTest(output=str(output), source=str(source)):
                with self.assertRaises(ValueError):
                    self.run_capture(client, destination=output, source=source)
        self.assertEqual(sentinel.read_text(), "keep")
        client.fetch.assert_not_called()

    def test_rate_limit_circuit_retains_all_unrequested_windows(self):
        client = FakeQuotesClient(error=MarketDataError("rate_limit", "Synthetic rate limit", http_status=429))
        report = self.run_capture(client)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(report["counts"]["skipped_request_segments"], 5)
        self.assertEqual(report["aggregate"]["expected_windows"], 6)
        self.assertEqual(report["aggregate"]["scenario_statuses"], {"DECISION_BLOCKED": 18})
        payloads = [json.loads(path.read_text()) for path in self.destination.glob("*-quotes.json")]
        self.assertEqual(sum(row["attempted"] for row in payloads), 1)
        self.assertTrue(all(row["observed_at"] is None and row["complete"] is False for row in payloads))
        self.assertTrue(all(row["records"] == [] for row in payloads))

    def test_capped_pagination_is_partial_and_never_quote_eligible(self):
        report = self.run_capture(FakeQuotesClient(complete=False))
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["aggregate"]["decision_states"], {"SOURCE_REJECTED": 6})
        self.assertEqual(report["aggregate"]["scenario_statuses"], {"DECISION_BLOCKED": 18})

    def test_changed_input_index_or_source_snapshot_never_reports_success(self):
        def mutate_index(client):
            if len(client.calls) == 1:
                path = self.source / "completion.json"
                path.write_bytes(path.read_bytes() + b" ")
        report = self.run_capture(FakeQuotesClient(after_fetch=mutate_index))
        self.assertEqual(report["status"], "source_changed")
        self.assertFalse(report["input_index_unchanged_during_run"])
        self.snapshot.side_effect = [SNAPSHOT, {"files": {"changed.py": {}}}]
        report = self.run_capture(FakeQuotesClient(), destination=self.base / "changed-source")
        self.assertEqual(report["status"], "source_changed")
        self.assertFalse(report["source_unchanged_during_run"])

    def test_source_file_modified_after_consumption_is_detected(self):
        def mutate_plan(client):
            if len(client.calls) == 1:
                path = self.source / "plan.json"
                path.write_bytes(path.read_bytes() + b" ")
        report = self.run_capture(FakeQuotesClient(after_fetch=mutate_plan))
        self.assertEqual(report["status"], "source_changed")
        self.assertFalse(report["input_index_unchanged_during_run"])

    def test_imports_do_not_load_broker_shadow_or_warehouse(self):
        script = """
import importlib.abc
import sys
class DenyTrading(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        forbidden = ('alpaca.trading', 'quantpaper.alpaca', 'quantpaper.shadow', 'quantpaper.warehouse')
        if any(fullname == name or fullname.startswith(name + '.') or
               (name == 'quantpaper.alpaca' and fullname.startswith(name + '_')) for name in forbidden):
            raise AssertionError('Trading or state module imported: ' + fullname)
        return None
sys.meta_path.insert(0, DenyTrading())
import quantpaper.marketdata.replay_runner
print('isolated imports verified')
"""
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parents[1], timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("isolated imports verified", result.stdout)


if __name__ == "__main__":
    unittest.main()
