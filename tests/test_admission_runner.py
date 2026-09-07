"""Offline admission evidence tests; all model/evidence mutations are temporary."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from quantpaper.admission_runner import run_admission
from quantpaper.marketdata.replay_book import analyze_replay_window
from quantpaper.marketdata.replay_protocol import build_replay_plan
from quantpaper.marketdata.storage import read_report, write_json
from quantpaper.marketdata.study_protocol import build_study_plan


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
GATES = {"ENABLE_ALPACA_PAPER": "NO", "ENABLE_ALPACA_PAPER_ROUND_TRIP": "NO"}
SNAPSHOT = {"files": {"synthetic.py": {"sha256": "a" * 64, "text": "# synthetic fixture\n"}}}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


class AdmissionRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        self.base = self.project / "artifacts/marketdata"
        self.source = self.base / "source-replay"
        self.run = self.base / "admission"
        self.source.mkdir(parents=True)
        self.env = self.project / ".env"
        self.env.write_text("ENABLE_ALPACA_PAPER=NO\nENABLE_ALPACA_PAPER_ROUND_TRIP=NO\n"
                            "APCA_API_KEY_ID=synthetic-admission-key-sentinel\n"
                            "APCA_API_SECRET_KEY=synthetic-admission-secret-sentinel\n")
        self.model_path = self.project / "artifacts/yahoo_walkforward_v2_model.joblib"
        self.model_path.write_bytes(b"This is deliberately not a deserializable model.")
        self.metadata_path = self.project / "artifacts/yahoo_walkforward_v2_model.json"
        self.metadata = {
            "model_sha256": digest(self.model_path.read_bytes()), "created_at": "2026-09-05T10:00:00Z",
            "feature_contract_version": "daily_pit_v2",
            "evaluation": {"approved_for_paper_signals": False, "rejection_reasons": ["synthetic gate denial"]},
        }
        self.metadata_path.write_text(json.dumps(self.metadata))
        self.protected = [self.env, self.model_path, self.metadata_path]
        for relative in ("data/research.duckdb", "artifacts/research-v3/frozen/manifest.json",
                         "artifacts/research-v3/frozen/report.json", "automation-fixture.toml"):
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"prior frozen evidence")
            self.protected.append(path)
        self._make_source()
        self.gates = patch.dict(os.environ, GATES)
        self.gates.start()
        self.addCleanup(self.gates.stop)
        self.snapshot = patch("quantpaper.admission_runner.source_snapshot", return_value=SNAPSHOT)
        self.snapshot.start()
        self.addCleanup(self.snapshot.stop)

    def _make_source(self, *, invalid_quote=False):
        study = build_study_plan(now=NOW - timedelta(hours=2), sessions=1, symbols=("SPY", "BTC/USD"))
        plan = build_replay_plan(study, now=NOW - timedelta(hours=1))
        payloads, rows = {}, []
        for window in plan["windows"]:
            target = datetime.fromisoformat(window["target"].replace("Z", "+00:00"))
            records = []
            for delay in (-500, 100, 900):
                quote = {"t": (target + timedelta(milliseconds=delay)).isoformat(),
                         "bp": 100., "ap": 100.02, "bs": 10, "as": 10}
                if window["symbol"] != "BTC/USD":
                    quote.update(c=["R"], z="A")
                if invalid_quote:
                    quote["ap"] = 99.
                records.append(quote)
            payload = {
                "symbol": window["symbol"], "kind": "quotes", "feed": window["feed"],
                "start": window["start"], "end": window["end"],
                "observed_at": (NOW - timedelta(hours=1)).isoformat(), "complete": True,
                "pages": 1, "records": records, "content_hash": digest(canonical(records)),
                "truncation_reason": None,
            }
            filename = f"{window['symbol'].replace('/', '-')}-{window['session']}-{window['window_name']}-quotes.json"
            row = analyze_replay_window(window["symbol"], payload, target=target, max_age_ms=1000,
                                        latencies_ms=(0, 250, 1000))
            row.update(session=window["session"], window_name=window["window_name"], raw_file=filename)
            rows.append(row)
            payloads[filename] = payload
        snapshot = {"files": {}, "scope": "synthetic source fixture"}
        for name, value in {"source.json": snapshot, "source-plan.json": study, **payloads}.items():
            (self.source / name).write_text(json.dumps(value, sort_keys=True))
        index = {path.name: digest(path.read_bytes()) for path in self.source.glob("*.json")
                 if path.name not in {"plan.json", "report.json", "completion.json"}}
        plan.update(source_snapshot_sha256=index["source.json"], source_study_plan_sha256=index["source-plan.json"])
        (self.source / "plan.json").write_text(json.dumps(plan, sort_keys=True))
        index["plan.json"] = digest((self.source / "plan.json").read_bytes())
        report = {
            "contract": "intraday_asof_replay_v1", "status": "captured", "execution_enabled": False,
            "source_unchanged_during_run": True, "input_index_unchanged_during_run": True,
            "plan_sha256": index["plan.json"], "source_snapshot_sha256": index["source.json"],
            "windows": rows,
        }
        (self.source / "report.json").write_text(json.dumps(report, sort_keys=True))
        index["report.json"] = digest((self.source / "report.json").read_bytes())
        (self.source / "completion.json").write_text(json.dumps({"status": "complete", "artifacts": index}))

    def hashes(self):
        paths = self.protected + list(self.source.glob("*.json"))
        return {str(path): digest(path.read_bytes()) for path in paths}

    def run_audit(self, *, run=None):
        return run_admission(run or self.run, self.source, project_root=self.project, now=NOW)

    def change_indexed(self, filename, change):
        path = self.source / filename
        content = json.loads(path.read_text())
        change(content)
        path.write_text(json.dumps(content, sort_keys=True))
        completion = json.loads((self.source / "completion.json").read_text())
        completion["artifacts"][filename] = digest(path.read_bytes())
        if filename == "plan.json":
            report = json.loads((self.source / "report.json").read_text())
            report["plan_sha256"] = completion["artifacts"][filename]
            (self.source / "report.json").write_text(json.dumps(report, sort_keys=True))
            completion["artifacts"]["report.json"] = digest((self.source / "report.json").read_bytes())
        (self.source / "completion.json").write_text(json.dumps(completion))

    def test_current_denied_model_rejects_all_intents_and_preserves_existing_evidence(self):
        before = self.hashes()
        report = self.run_audit()
        self.assertEqual(report["status"], "audited")
        self.assertEqual(report["counts"], {"source_windows": 6, "synthetic_intents": 36,
                                          "unique_intent_ids": 36, "audit_events": 108,
                                          "states": {"REJECTED": 36}})
        self.assertEqual(report["rejection_reason_counts"], {"MODEL_NOT_APPROVED": 36})
        self.assertEqual(report["model_evidence"]["approved_for_paper"], False)
        self.assertEqual(report["orders_submitted"], 0)
        self.assertEqual(report["fills_simulated"], 0)
        self.assertEqual(report["network_requests"], 0)
        self.assertFalse(report["execution_enabled"])
        self.assertEqual(self.hashes(), before)
        self.assertEqual(report, read_report(self.run, self.project))
        all_bytes = b"".join(path.read_bytes() for path in self.run.glob("*.json"))
        self.assertNotIn(b"synthetic-admission-key-sentinel", all_bytes)
        self.assertNotIn(b"synthetic-admission-secret-sentinel", all_bytes)
        self.assertNotIn(b"deliberately not a deserializable model", all_bytes)

    def test_chain_has_actual_observation_time_and_valid_hash_links(self):
        report = self.run_audit()
        chain = json.loads((self.run / "events.json").read_text())
        previous = "0" * 64
        for sequence, event in enumerate(chain["events"], start=1):
            self.assertEqual(event["sequence"], sequence)
            self.assertEqual(event["previous_sha256"], previous)
            self.assertEqual(event["payload"]["run_observed_at"], NOW.isoformat())
            self.assertEqual(event["sha256"], digest(canonical({key: value for key, value in event.items() if key != "sha256"})))
            previous = event["sha256"]
        self.assertEqual(previous, chain["head_sha256"])
        self.assertEqual(previous, report["event_chain_head_sha256"])
        logical = [event["payload"]["logical_event"]["event"] for event in chain["events"]]
        self.assertEqual(logical[:3], ["INTENT_CREATED", "CHECKS_COMPLETED", "REJECTED"])
        self.assertFalse(any(name in {"SUBMITTED", "FILLED"} for name in logical))

    def test_quote_failures_are_kept_alongside_model_rejection(self):
        self._make_source(invalid_quote=True)
        report = self.run_audit()
        self.assertEqual(report["rejection_reason_counts"]["MODEL_NOT_APPROVED"], 36)
        self.assertTrue(any(reason.startswith("QUOTE_") for reason in report["rejection_reason_counts"]))
        self.assertTrue(all(value <= 36 for value in report["rejection_reason_counts"].values()))

    def test_approved_fixture_only_permits_offline_simulation_never_submits(self):
        self.metadata["evaluation"] = {"approved_for_paper_signals": True, "rejection_reasons": []}
        self.metadata_path.write_text(json.dumps(self.metadata))
        report = self.run_audit()
        self.assertEqual(report["counts"]["states"], {"APPROVED_FOR_SIMULATION_ONLY": 36})
        self.assertEqual(report["orders_submitted"], 0)
        self.assertEqual(report["fills_simulated"], 0)
        self.assertFalse(report["execution_enabled"])

    def test_ids_are_source_bound_and_repeat_deterministically_in_new_run(self):
        self.run_audit()
        second = self.base / "admission-second"
        self.run_audit(run=second)
        first_ids = [row["intent"]["id"] for row in json.loads((self.run / "intents.json").read_text())["intents"]]
        second_ids = [row["intent"]["id"] for row in json.loads((second / "intents.json").read_text())["intents"]]
        self.assertEqual(first_ids, second_ids)
        expected_source = digest((self.source / "completion.json").read_bytes())
        rows = json.loads((second / "intents.json").read_text())["intents"]
        self.assertTrue(all(row["intent"]["source_completion_sha256"] == expected_source for row in rows))

    def test_duplicate_id_fails_without_completed_artifact(self):
        with patch("quantpaper.admission_runner.deterministic_intent_id", return_value="synthetic-v1-" + "a" * 64):
            with self.assertRaisesRegex(ValueError, "duplicate"):
                self.run_audit()
        self.assertFalse((self.run / "completion.json").exists())

    def test_bad_model_hash_and_nonboolean_approval_fail_before_output(self):
        for field, value in (("model_sha256", "0" * 64), ("approval", "false")):
            bad = deepcopy(self.metadata)
            if field == "approval":
                bad["evaluation"]["approved_for_paper_signals"] = value
            else:
                bad[field] = value
            self.metadata_path.write_text(json.dumps(bad))
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.run_audit()
            self.assertFalse(self.run.exists())

    def test_source_row_summary_cannot_override_raw_reanalysis_even_after_reindexing(self):
        self.change_indexed("report.json", lambda report: report["windows"][0]["decision_state"].update(status="FORGED_VALID"))
        with self.assertRaisesRegex(ValueError, "fresh raw-quote reanalysis"):
            self.run_audit()
        self.assertFalse(self.run.exists())

    def test_raw_feed_mismatch_is_rejected_even_with_valid_file_and_content_hashes(self):
        filename = next(path.name for path in self.source.glob("SPY-*-quotes.json"))
        self.change_indexed(filename, lambda raw: raw.update(feed="iex"))
        with self.assertRaisesRegex(ValueError, "identity"):
            self.run_audit()
        self.assertFalse(self.run.exists())

    def test_source_plan_and_report_hash_crosslinks_are_required(self):
        self.change_indexed("report.json", lambda report: report.update(plan_sha256="f" * 64))
        with self.assertRaisesRegex(ValueError, "paired"):
            self.run_audit()
        self.assertFalse(self.run.exists())

    def test_plan_changes_and_missing_rows_are_rejected(self):
        self.change_indexed("plan.json", lambda plan: plan["policy"].update(max_age_ms=1000.0))
        with self.assertRaisesRegex(ValueError, "protocol"):
            self.run_audit()
        self.assertFalse(self.run.exists())

    def test_dirty_code_or_model_is_detected_before_completion(self):
        with patch("quantpaper.admission_runner.source_snapshot", side_effect=[SNAPSHOT, {"changed": True}]):
            with self.assertRaisesRegex(ValueError, "changed during"):
                self.run_audit()
        self.assertTrue((self.run / "plan.json").exists())
        self.assertFalse((self.run / "completion.json").exists())
        self.assertFalse((self.run / "events.json").exists())

    def test_model_mutated_midrun_fails_closed(self):
        from quantpaper.admission import evaluate_intent

        def mutate(*args, **kwargs):
            self.model_path.write_bytes(b"changed synthetic model bytes")
            return evaluate_intent(*args, **kwargs)

        with patch("quantpaper.admission_runner.evaluate_intent", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "model bytes"):
                self.run_audit()
        self.assertFalse((self.run / "completion.json").exists())

    def test_open_gates_and_existing_output_fail_without_mutation(self):
        before = self.hashes()
        with patch.dict(os.environ, {"ENABLE_ALPACA_PAPER": "YES"}):
            with self.assertRaises(RuntimeError):
                self.run_audit()
        self.assertFalse(self.run.exists())
        self.run.mkdir()
        (self.run / "existing.json").write_bytes(b"existing evidence")
        with self.assertRaises(ValueError):
            self.run_audit()
        self.assertEqual((self.run / "existing.json").read_bytes(), b"existing evidence")
        self.assertEqual(self.hashes(), before)

    def test_symlinked_model_is_not_followed(self):
        target = self.project / "synthetic-model-target"
        target.write_bytes(self.model_path.read_bytes())
        self.model_path.unlink()
        self.model_path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            self.run_audit()
        self.assertFalse(self.run.exists())

    def test_naive_now_rejected_without_creating_output(self):
        with self.assertRaises(ValueError):
            run_admission(self.run, self.source, project_root=self.project, now=datetime(2026, 9, 7))
        self.assertFalse(self.run.exists())


class AdmissionImportIsolationTests(unittest.TestCase):
    def test_import_has_no_http_broker_model_deserialization_or_environment_mutation(self):
        script = textwrap.dedent("""
            import importlib.abc, hashlib, json, os, socket, sys
            forbidden = ('requests', 'alpaca', 'joblib', 'sklearn', 'yfinance', 'duckdb',
                         'quantpaper.marketdata.client', 'quantpaper.marketdata.replay_runner',
                         'quantpaper.shadow', 'quantpaper.alpaca_paper')
            class Guard(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if any(fullname == item or fullname.startswith(item + '.') for item in forbidden):
                        raise AssertionError('Forbidden offline admission import: ' + fullname)
            def no_network(*args, **kwargs):
                raise AssertionError('Offline admission attempted network')
            def env_hash():
                return hashlib.sha256(json.dumps(dict(os.environ), sort_keys=True).encode()).hexdigest()
            before = env_hash()
            sys.meta_path.insert(0, Guard())
            socket.create_connection = no_network
            socket.socket.connect = no_network
            from quantpaper.admission_runner import run_admission
            assert env_hash() == before, 'Offline admission import changed environment'
        """)
        environment = dict(os.environ)
        environment.update(PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1", OPENBLAS_NUM_THREADS="1")
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-c", script], cwd=directory, env=environment,
                                    text=True, capture_output=True, timeout=40)
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
