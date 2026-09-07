"""Evidence CLI isolation and archived integrity using synthetic packets only."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
from datetime import datetime, timezone
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
from unittest.mock import patch

from quantpaper.research import evidence_cli as cli
from quantpaper.research.evidence import (
    EvidenceRequest, build_evidence_packet, canonical_bytes, digest, packet_summary,
)


ROOT = Path(__file__).resolve().parents[1]


class EvidenceCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name).resolve()
        self.database = self.project / "data/research.duckdb"
        self.database.parent.mkdir()
        self.database.write_bytes(b"synthetic database sentinel; never opened\n")
        self.protected = [self.database]
        for name in (".env", "artifacts/ml-v2/model.joblib", "data/shadow.duckdb",
                     "configs/paper.toml", "src/quantpaper/research/evidence.py"):
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic protected local state\n")
            self.protected.append(path)
        self.before = {path: path.read_bytes() for path in self.protected}
        self.run_dir = self.project / "artifacts/research-evidence/synthetic-run"
        self.request = EvidenceRequest(
            instrument_id="YAHOO:SPY", as_of="2026-09-07T12:00:00Z", macro_series=(),
        )
        known = {"available_at": "2026-09-05T12:00:00Z", "ingested_at": "2026-09-05T13:00:00Z"}
        snapshot = {
            "identity": [{**known, "instrument_id": "YAHOO:SPY", "symbol": "SPY",
                          "asset_class": "equity", "primary_exchange": "ARCX", "source": "yahoo",
                          "valid_from": "2020-01-01T00:00:00Z", "valid_to": None}],
            "market": [{**known, "instrument_id": "YAHOO:SPY", "event_time": "2026-09-01T00:00:00Z",
                        "interval": "1d", "open": 100.0, "high": 102.0, "low": 99.0,
                        "close": 101.0, "volume": 1000.0, "source": "yahoo",
                        "data_version": "snapshot-v2:synthetic"}],
            "fundamentals": [], "macro": {}, "news": [],
        }
        with patch("quantpaper.research.evidence_store.read_evidence_snapshot", return_value=snapshot):
            self.packet = build_evidence_packet(
                self.database, self.request, generated_at=datetime(2026, 9, 7, 13, tzinfo=timezone.utc),
            )
        files = {name: digest({"synthetic_source": name})
                 for name in ("evidence.py", "evidence_store.py", "evidence_cli.py")}
        self.fingerprint = {"files": files, "sha256": digest(files)}

    def build(self, run_dir=None):
        with (patch.object(cli, "build_evidence_packet", return_value=copy.deepcopy(self.packet)) as builder,
              patch.object(cli, "_code_fingerprint", return_value=copy.deepcopy(self.fingerprint))):
            result = cli.build_run(self.database, self.request, run_dir or self.run_dir,
                                   project_root=self.project)
        builder.assert_called_once_with(self.database, self.request)
        return result

    def read(self, run_dir=None):
        return cli.read_run(run_dir or self.run_dir, project_root=self.project)

    def write_json(self, name, value):
        (self.run_dir / name).write_text(json.dumps(value, sort_keys=True, allow_nan=False) + "\n")

    def load_json(self, name):
        return json.loads((self.run_dir / name).read_bytes())

    def reindex(self, name):
        completion = self.load_json("completion.json")
        completion["artifacts"][name] = hashlib.sha256((self.run_dir / name).read_bytes()).hexdigest()
        self.write_json("completion.json", completion)

    def assert_protected_unchanged(self):
        self.assertEqual({path: path.read_bytes() for path in self.protected}, self.before)

    def test_build_is_create_only_isolated_and_show_verifies_all_hashes(self):
        self.assertEqual(self.build(), self.packet)
        self.assertEqual({path.name for path in self.run_dir.iterdir()},
                         {"packet.json", "report.md", "manifest.json", "completion.json"})
        completion = self.load_json("completion.json")
        self.assertEqual(completion["status"], "complete")
        for name, expected in completion["artifacts"].items():
            self.assertEqual(hashlib.sha256((self.run_dir / name).read_bytes()).hexdigest(), expected)
        self.assertEqual(canonical_bytes(self.read()), canonical_bytes(self.packet))
        self.assertFalse(self.read()["execution_enabled"])
        self.assert_protected_unchanged()

    def test_relative_run_path_is_scoped_to_explicit_project_root(self):
        self.build(Path("artifacts/research-evidence/synthetic-run"))
        self.assertEqual(self.read()["packet_hash"], self.packet["packet_hash"])
        self.assert_protected_unchanged()

    def test_outside_base_and_traversal_are_rejected_before_packet_builder(self):
        invalid = (self.project, self.project / "artifacts/research-evidence",
                   self.project / "artifacts/research-v3/wrong", Path("unscoped-run"),
                   Path("artifacts/research-evidence/../escaped"),
                   self.project / "artifacts/research-evidence/a/../b")
        for run_dir in invalid:
            with self.subTest(path=str(run_dir)), patch.object(cli, "build_evidence_packet") as builder:
                with self.assertRaises(ValueError):
                    cli.build_run(self.database, self.request, run_dir, project_root=self.project)
                with self.assertRaises(ValueError):
                    cli.read_run(run_dir, project_root=self.project)
                builder.assert_not_called()
        self.assertFalse(self.run_dir.exists())
        self.assert_protected_unchanged()

    def test_existing_run_preserved_without_rebuilding(self):
        self.build()
        before = {path.name: path.read_bytes() for path in self.run_dir.iterdir()}
        with patch.object(cli, "build_evidence_packet") as builder:
            with self.assertRaisesRegex(ValueError, "already exists"):
                cli.build_run(self.database, self.request, self.run_dir, project_root=self.project)
            builder.assert_not_called()
        self.assertEqual({path.name: path.read_bytes() for path in self.run_dir.iterdir()}, before)
        self.assert_protected_unchanged()

    def test_symlink_run_or_ancestors_are_rejected_even_when_dangling(self):
        for linked_name in ("artifacts", "artifacts/research-evidence", "artifacts/research-evidence/run"):
            for dangling in (False, True):
                with self.subTest(linked_name=linked_name, dangling=dangling), tempfile.TemporaryDirectory() as directory:
                    project = Path(directory).resolve()
                    target = project / "unpublished"
                    if not dangling:
                        target.mkdir()
                        (target / "protected.txt").write_text("unchanged")
                    link = project / linked_name
                    link.parent.mkdir(parents=True, exist_ok=True)
                    link.symlink_to(target, target_is_directory=True)
                    run_dir = project / "artifacts/research-evidence/run"
                    with patch.object(cli, "build_evidence_packet") as builder:
                        with self.assertRaises(ValueError):
                            cli.build_run(self.database, self.request, run_dir, project_root=project)
                        with self.assertRaises(ValueError):
                            cli.read_run(run_dir, project_root=project)
                        builder.assert_not_called()
                    if not dangling:
                        self.assertEqual((target / "protected.txt").read_text(), "unchanged")
                        self.assertEqual(len(list(target.iterdir())), 1)

    def test_packet_builder_failure_or_source_change_publishes_nothing(self):
        with patch.object(cli, "build_evidence_packet", side_effect=ValueError("synthetic failure")):
            with self.assertRaises(ValueError):
                cli.build_run(self.database, self.request, self.run_dir, project_root=self.project)
        self.assertFalse(self.run_dir.exists())
        changed = {"files": {}, "sha256": digest({})}
        with (patch.object(cli, "build_evidence_packet", return_value=self.packet),
              patch.object(cli, "_code_fingerprint", side_effect=[self.fingerprint, changed])):
            with self.assertRaisesRegex(ValueError, "code changed"):
                cli.build_run(self.database, self.request, self.run_dir, project_root=self.project)
        self.assertFalse(self.run_dir.exists())
        self.assert_protected_unchanged()

    def test_failed_write_preserves_partial_run_without_completion_or_overwrite(self):
        create = cli._create_bytes

        def fail_on_report(path, payload):
            if path.name == "report.md":
                raise OSError("synthetic disk failure")
            create(path, payload)

        with patch.object(cli, "_create_bytes", side_effect=fail_on_report):
            with self.assertRaises(OSError):
                self.build()
        self.assertEqual({path.name for path in self.run_dir.iterdir()}, {"packet.json"})
        packet_bytes = (self.run_dir / "packet.json").read_bytes()
        with self.assertRaises((ValueError, OSError)):
            self.read()
        with self.assertRaises(ValueError):
            self.build()
        self.assertEqual((self.run_dir / "packet.json").read_bytes(), packet_bytes)

    def test_each_indexed_artifact_tamper_is_detected(self):
        self.build()
        for name in sorted(cli.ARTIFACT_NAMES):
            with self.subTest(name=name):
                path = self.run_dir / name
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                with self.assertRaisesRegex(ValueError, "integrity mismatch"):
                    self.read()
                path.write_bytes(original)

    def test_reindexed_packet_tamper_still_fails_its_internal_hash(self):
        self.build()
        packet = self.load_json("packet.json")
        packet["sections"]["market"]["records"][0]["close"] = 100.25
        self.write_json("packet.json", packet)
        self.reindex("packet.json")
        with self.assertRaisesRegex(ValueError, "packet hash mismatch"):
            self.read()

    def test_reindexed_manifest_tamper_still_fails_its_internal_hash(self):
        self.build()
        manifest = self.load_json("manifest.json")
        manifest["runtime"]["python"] = "synthetic-changed-runtime"
        self.write_json("manifest.json", manifest)
        self.reindex("manifest.json")
        with self.assertRaisesRegex(ValueError, "manifest hash mismatch"):
            self.read()

    def test_manifest_crosslink_and_builder_fingerprint_are_independently_checked(self):
        self.build()
        original = self.load_json("manifest.json")
        for field in ("packet_hash", "snapshot_hash", "request_hash", "builder"):
            with self.subTest(field=field):
                manifest = copy.deepcopy(original)
                if field == "builder":
                    manifest["builder"]["files"]["evidence.py"] = "0" * 64
                else:
                    manifest[field] = "0" * 64
                manifest["manifest_hash"] = digest({key: value for key, value in manifest.items()
                                                     if key != "manifest_hash"})
                self.write_json("manifest.json", manifest)
                completion = self.load_json("completion.json")
                completion["manifest_hash"] = manifest["manifest_hash"]
                self.write_json("completion.json", completion)
                self.reindex("manifest.json")
                with self.assertRaises(ValueError):
                    self.read()

    def test_incomplete_or_wrong_completion_is_rejected(self):
        self.build()
        original = self.load_json("completion.json")
        for changes in ({"status": "partial"}, {"schema_version": 2},
                        {"packet_hash": "0" * 64}, {"manifest_hash": "0" * 64}):
            with self.subTest(changes=changes):
                self.write_json("completion.json", {**original, **changes})
                with self.assertRaises(ValueError):
                    self.read()
        (self.run_dir / "completion.json").unlink()
        with self.assertRaises((ValueError, OSError)):
            self.read()

    def test_completion_path_injection_is_rejected_before_external_artifact_read(self):
        self.build()
        original = self.load_json("completion.json")
        for name in ("../protected.txt", "/tmp/untrusted-evidence", "nested/packet.json"):
            with self.subTest(name=name):
                completion = copy.deepcopy(original)
                completion["artifacts"][name] = "0" * 64
                self.write_json("completion.json", completion)
                with patch.object(cli, "_read_bytes", wraps=cli._read_bytes) as reader:
                    with self.assertRaisesRegex(ValueError, "artifact list"):
                        self.read()
                reader.assert_called_once_with(self.run_dir / "completion.json")

    def test_artifact_symlink_is_rejected_without_following_target(self):
        self.build()
        for name in ("completion.json", *sorted(cli.ARTIFACT_NAMES)):
            with self.subTest(name=name):
                path = self.run_dir / name
                payload = path.read_bytes()
                target = self.project / "unpublished-artifact"
                target.write_bytes(payload)
                path.unlink()
                path.symlink_to(target)
                with self.assertRaises(ValueError):
                    self.read()
                self.assertEqual(target.read_bytes(), payload)
                path.unlink()
                path.write_bytes(payload)

    def test_show_survives_original_database_and_source_changes_without_rebuilding(self):
        self.build()
        expected = canonical_bytes(self.packet)
        before = {path.name: path.read_bytes() for path in self.run_dir.iterdir()}
        self.database.write_bytes(b"later synthetic database revision\n")
        (self.project / "src/quantpaper/research/evidence.py").unlink()
        with (patch.object(cli, "build_evidence_packet", side_effect=AssertionError("must not rebuild")),
              patch.object(cli, "_code_fingerprint", side_effect=AssertionError("must not inspect current code")),
              patch("quantpaper.research.evidence_store.read_evidence_snapshot",
                    side_effect=AssertionError("must not open the database"))):
            self.assertEqual(canonical_bytes(self.read()), expected)
        self.assertEqual({path.name: path.read_bytes() for path in self.run_dir.iterdir()}, before)

    def test_cli_build_dispatch_and_show_only_print_nonexecutable_summary(self):
        args = ["build", "--db", "synthetic.duckdb", "--instrument-id", "YAHOO:SPY",
                "--as-of", "2026-09-07T12:00:00Z", "--series", "--run-dir", "isolated-run"]
        output = io.StringIO()
        with patch.object(cli, "build_run", return_value=self.packet) as builder, redirect_stdout(output):
            self.assertEqual(cli.main(args), 0)
        database, request, run_dir = builder.call_args.args
        self.assertEqual((database, request, run_dir),
                         (Path("synthetic.duckdb"), self.request, Path("isolated-run")))
        self.assertEqual(json.loads(output.getvalue()), packet_summary(self.packet))
        output = io.StringIO()
        with patch.object(cli, "read_run", return_value=self.packet) as reader, redirect_stdout(output):
            self.assertEqual(cli.main(["show", "--run-dir", "isolated-run"]), 0)
        reader.assert_called_once_with(Path("isolated-run"))
        self.assertFalse(json.loads(output.getvalue())["execution_enabled"])

    def test_cli_errors_are_sanitized_without_printing_untrusted_payload(self):
        sentinel = "synthetic-untrusted-payload-not-for-output"
        output, errors = io.StringIO(), io.StringIO()
        with (patch.object(cli, "read_run", side_effect=ValueError(sentinel)),
              redirect_stdout(output), redirect_stderr(errors)):
            self.assertEqual(cli.main(["show", "--run-dir", "isolated-run"]), 2)
        self.assertNotIn(sentinel, output.getvalue() + errors.getvalue())
        self.assertIn("Evidence command failed", errors.getvalue())

    def test_malformed_archived_json_shapes_fail_with_sanitized_cli_error(self):
        self.build()
        original = {path.name: path.read_bytes() for path in self.run_dir.iterdir()}
        sentinel = "synthetic-untrusted-archive-value"
        malformed = (("completion.json", [sentinel]), ("completion.json", None),
                     ("manifest.json", [sentinel]), ("packet.json", [sentinel]))
        reader = cli.read_run
        for name, value in malformed:
            with self.subTest(name=name, kind=type(value).__name__):
                for filename, payload in original.items():
                    (self.run_dir / filename).write_bytes(payload)
                self.write_json(name, value)
                if name != "completion.json":
                    self.reindex(name)
                output, errors = io.StringIO(), io.StringIO()
                with (patch.object(cli, "read_run", side_effect=lambda path: reader(path, project_root=self.project)),
                      redirect_stdout(output), redirect_stderr(errors)):
                    self.assertEqual(cli.main(["show", "--run-dir", str(self.run_dir)]), 2)
                self.assertNotIn(sentinel, output.getvalue() + errors.getvalue())
                self.assertIn("Evidence command failed", errors.getvalue())

    def test_fresh_help_and_verified_show_have_no_data_broker_or_network_imports(self):
        self.build()
        self.database.unlink()
        script = textwrap.dedent("""
            import importlib.abc
            import json
            import os
            from pathlib import Path
            import runpy
            import socket
            import sys

            forbidden = (
                "alpaca", "requests", "httpx", "urllib.request", "dotenv", "duckdb",
                "yfinance", "joblib", "sklearn", "pandas", "quantpaper.alpaca",
                "quantpaper.alpaca_cli", "quantpaper.alpaca_paper", "quantpaper.shadow",
                "quantpaper.shadow_cli", "quantpaper.warehouse", "quantpaper.sources",
                "quantpaper.research.evidence_store",
            )
            class NoUnsafeImports(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if any(fullname == name or fullname.startswith(name + ".") for name in forbidden):
                        raise AssertionError("evidence help/show imported forbidden module: " + fullname)
                    return None

            def no_network(*args, **kwargs):
                raise AssertionError("evidence help/show attempted a network connection")

            sys.meta_path.insert(0, NoUnsafeImports())
            socket.create_connection = no_network
            socket.socket.connect = no_network
            before = dict(os.environ)
            from quantpaper.research import evidence_cli as cli
            try:
                cli.main(["--help"])
            except SystemExit as error:
                if error.code != 0:
                    raise
            else:
                raise AssertionError("help did not exit through argparse")
            root, run_dir = Path(sys.argv[1]), Path(sys.argv[2])
            launcher = sys.argv[3]
            sys.argv = [launcher, "evidence", "--help"]
            try:
                runpy.run_path(launcher, run_name="__main__")
            except SystemExit as error:
                if error.code != 0:
                    raise
            else:
                raise AssertionError("launcher evidence help did not exit through argparse")
            reader = cli.read_run
            cli.read_run = lambda path: reader(path, project_root=root)
            if cli.main(["show", "--run-dir", str(run_dir)]) != 0:
                raise AssertionError("verified show failed")
            if before != dict(os.environ):
                raise AssertionError("evidence help/show changed environment variables")
            if any(name in sys.modules for name in forbidden):
                raise AssertionError("a forbidden dependency was loaded")
        """)
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run([sys.executable, "-c", script, str(self.project), str(self.run_dir),
                                 str(ROOT / "main.py")],
                                cwd=self.project, env=environment, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn('"execution_enabled": false', result.stdout)


if __name__ == "__main__":
    unittest.main()
