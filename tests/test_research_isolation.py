"""Boundary regressions for research output and its non-broker import graph."""

from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

from quantpaper.research.cli import read_report, validate_run_directory
from quantpaper.research.protocol import freeze_manifest, load_protocol


ROOT = Path(__file__).resolve().parents[1]


class ResearchOutputIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name).resolve() / "project"
        self.output_base = self.project / "artifacts" / "research-v3"
        self.output_base.mkdir(parents=True)

    def test_new_child_run_directory_is_accepted(self) -> None:
        validate_run_directory(self.output_base / "run-001", self.project)

    def test_existing_run_is_rejected_and_evidence_preserved(self) -> None:
        run_dir = self.output_base / "existing"
        run_dir.mkdir()
        report = run_dir / "report.json"
        report.write_bytes(b'{"sentinel":"original research evidence"}\n')
        before = report.read_bytes()
        with self.assertRaises(ValueError):
            validate_run_directory(run_dir, self.project)
        self.assertEqual(report.read_bytes(), before)

    def test_protected_output_roots_and_path_traversal_are_rejected(self) -> None:
        forbidden = (
            self.project,
            self.project / "artifacts",
            self.output_base,
            self.project / "data" / "research.duckdb",
            self.output_base / ".." / "yahoo_walkforward_v2_model.joblib",
            Path(self.temporary.name) / "outside-project",
        )
        for candidate in forbidden:
            with self.subTest(path=str(candidate)):
                with self.assertRaises(ValueError):
                    validate_run_directory(candidate, self.project)
        self.assertFalse((self.project / "data").exists())
        self.assertFalse((self.project / "artifacts" / "yahoo_walkforward_v2_model.joblib").exists())

    def test_symlink_to_outside_output_tree_is_rejected(self) -> None:
        protected = self.project / "data"
        protected.mkdir()
        sentinel = protected / "research.duckdb"
        sentinel.write_bytes(b"not-a-real-database; immutable sentinel")
        alias = self.output_base / "alias"
        alias.symlink_to(protected, target_is_directory=True)
        with self.assertRaises(ValueError):
            validate_run_directory(alias / "new-run", self.project)
        self.assertFalse((protected / "new-run").exists())
        self.assertEqual(sentinel.read_bytes(), b"not-a-real-database; immutable sentinel")

    def test_symlink_ancestor_is_rejected_even_if_target_stays_in_output_tree(self) -> None:
        physical = self.output_base / "physical"
        physical.mkdir()
        alias = self.output_base / "alias"
        alias.symlink_to(physical, target_is_directory=True)
        with self.assertRaises(ValueError):
            validate_run_directory(alias / "new-run", self.project)
        self.assertFalse((physical / "new-run").exists())

    def test_symlink_artifacts_ancestor_is_rejected(self) -> None:
        alternate_project = Path(self.temporary.name).resolve() / "other-project"
        alternate_project.mkdir()
        (alternate_project / "artifacts").symlink_to(self.project / "artifacts", target_is_directory=True)
        with self.assertRaises(ValueError):
            validate_run_directory(alternate_project / "artifacts" / "research-v3" / "new-run", alternate_project)
        self.assertFalse((self.output_base / "new-run").exists())

    def historical_run(self) -> tuple[Path, Path, Path, dict]:
        input_path = self.project / "historical-input.csv"
        code_path = self.project / "historical-code.py"
        input_path.write_bytes(b"timestamp,close\n2026-09-01,100\n")
        code_path.write_bytes(b"# frozen historical research code\n")
        run_dir = self.output_base / "historical-run"
        protocol = load_protocol(ROOT / "configs" / "research_v3.toml")
        manifest = freeze_manifest(run_dir, protocol, [input_path], [code_path])
        report = {"research_only": True, "snapshot_hash": manifest["snapshot_hash"], "groups": {}}
        payloads = {
            "report.json": json.dumps(report).encode(),
            "predictions.csv": b"timestamp,probability\n2026-09-01,0.5\n",
        }
        for name, payload in payloads.items():
            (run_dir / name).write_bytes(payload)
        (run_dir / "completion.json").write_text(json.dumps({
            "status": "complete", "manifest_hash": manifest["manifest_hash"],
            "artifacts": {name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()},
        }))
        return run_dir, input_path, code_path, report

    def test_historical_report_checks_stored_manifest_integrity(self) -> None:
        run_dir, _, _, _ = self.historical_run()
        manifest_path = run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["input_files"][0]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "Manifest integrity hash mismatch"):
            read_report(run_dir, self.project)

    def test_historical_report_does_not_require_current_inputs_or_code_to_match(self) -> None:
        run_dir, input_path, code_path, report = self.historical_run()
        input_path.write_bytes(b"a later research input revision\n")
        code_path.unlink()
        self.assertEqual(read_report(run_dir, self.project), report)


class ResearchDependencyIsolationTests(unittest.TestCase):
    def test_fresh_import_and_help_do_not_load_broker_or_shadow_database(self) -> None:
        # A fresh interpreter matters: preloaded modules in the main test suite
        # could otherwise conceal an unsafe import behind sys.modules caching.
        script = textwrap.dedent("""
            import importlib.abc
            import os
            import socket
            import sys

            forbidden = (
                "alpaca", "duckdb", "quantpaper.alpaca", "quantpaper.alpaca_cli",
                "quantpaper.alpaca_paper", "quantpaper.sources.alpaca_news",
                "quantpaper.shadow", "quantpaper.shadow_cli", "quantpaper.warehouse",
            )
            class NoExecutionImports(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if any(fullname == name or fullname.startswith(name + ".") for name in forbidden):
                        raise AssertionError("research loaded forbidden module: " + fullname)
                    return None

            def no_network(*args, **kwargs):
                raise AssertionError("research attempted a network connection")

            sys.meta_path.insert(0, NoExecutionImports())
            socket.create_connection = no_network
            socket.socket.connect = no_network
            before = dict(os.environ)
            from quantpaper.research import cli
            try:
                cli.main(["--help"])
            except SystemExit as error:
                if error.code != 0:
                    raise
            else:
                raise AssertionError("research help did not exit through argparse")
            if before != dict(os.environ):
                raise AssertionError("research import/help mutated environment variables")
        """)
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["OPENBLAS_NUM_THREADS"] = "1"
        # sklearn sets these process defaults on import; establish them before
        # the snapshot so the test detects research mutations, not library setup.
        environment.setdefault("KMP_DUPLICATE_LIB_OK", "True")
        environment.setdefault("KMP_INIT_AT_FORK", "FALSE")
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=directory, env=environment,
                text=True, capture_output=True, timeout=40,
            )
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
