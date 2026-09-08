"""Feature CLI contract tests with a synthetic packet API and no live inputs."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from quantpaper.research import feature_cli


class FeatureCliTests(unittest.TestCase):
    def setUp(self):
        self.api = ModuleType("quantpaper.research.feature_packet")
        self.api.build_run = Mock(return_value={"status": "COMPLETE", "private_payload": "must-not-print"})
        self.api.read_run = Mock(return_value={"status": "COMPLETE", "private_payload": "must-not-print"})
        self.api.packet_summary = Mock(return_value={"status": "COMPLETE", "research_only": True,
                                                     "execution_enabled": False, "packet_hash": "a" * 64})
        self.project = Path("/synthetic/project")
        self.arguments = ["build", "--catalog-run-dir", "artifacts/research-catalog/existing",
                          "--as-of", "2026-09-08T12:00:00Z", "--run-dir", "artifacts/research-features/new"]

    def invoke(self, arguments):
        output, error = io.StringIO(), io.StringIO()
        with (patch.dict(sys.modules, {self.api.__name__: self.api}),
              patch.object(feature_cli, "PROJECT_ROOT", self.project),
              redirect_stdout(output), redirect_stderr(error)):
            result = feature_cli.main(arguments)
        return result, output.getvalue(), error.getvalue()

    def test_build_passes_only_the_bounded_api_arguments_with_defaults(self):
        result, output, error = self.invoke(self.arguments)
        self.assertEqual(result, 0)
        self.assertEqual(error, "")
        self.api.build_run.assert_called_once_with(
            Path("data/research.duckdb"), Path("artifacts/research-catalog/existing"),
            Path("artifacts/research-features/new"), project_root=self.project,
            instrument_id="YF:JPM", as_of="2026-09-08T12:00:00.000000+00:00")
        self.api.read_run.assert_not_called()
        self.assertEqual(json.loads(output), self.api.packet_summary.return_value)
        self.assertNotIn("must-not-print", output)

    def test_explicit_database_instrument_and_offset_cutoff_are_forwarded(self):
        args = ["build", "--db", "data/another.duckdb", "--instrument-id", "YF:BAC",
                "--catalog-run-dir", "artifacts/research-catalog/existing", "--as-of", "2026-09-08T13:00:00+01:00",
                "--run-dir", "artifacts/research-features/new"]
        self.assertEqual(self.invoke(args)[0], 0)
        self.api.build_run.assert_called_once_with(
            Path("data/another.duckdb"), Path("artifacts/research-catalog/existing"),
            Path("artifacts/research-features/new"), project_root=self.project,
            instrument_id="YF:BAC", as_of="2026-09-08T12:00:00.000000+00:00")

    def test_partial_build_returns_three_after_printing_summary(self):
        self.api.build_run.return_value = {"status": "PARTIAL"}
        self.api.packet_summary.return_value["status"] = "PARTIAL"
        result, output, error = self.invoke(self.arguments)
        self.assertEqual((result, error), (3, ""))
        self.assertEqual(json.loads(output)["status"], "PARTIAL")

    def test_verified_show_returns_zero_for_partial_and_complete_without_building(self):
        for status in ("PARTIAL", "COMPLETE"):
            with self.subTest(status=status):
                self.api.read_run.reset_mock()
                self.api.read_run.return_value = {"status": status}
                result, output, error = self.invoke(["show", "--run-dir", "artifacts/research-features/archived"])
                self.assertEqual((result, error), (0, ""))
                self.api.read_run.assert_called_once_with(Path("artifacts/research-features/archived"), project_root=self.project)
                self.api.build_run.assert_not_called()
                self.assertIs(json.loads(output)["execution_enabled"], False)

    def test_build_and_show_exceptions_are_sanitized_with_no_stdout(self):
        secret = "private-authenticated-url-and-credential"
        for name, args in (("build_run", self.arguments),
                           ("read_run", ["show", "--run-dir", "artifacts/research-features/archived"])):
            with self.subTest(name=name):
                getattr(self.api, name).side_effect = RuntimeError(secret)
                result, output, error = self.invoke(args)
                self.assertEqual((result, output), (2, ""))
                self.assertEqual(error, feature_cli.FAILURE_MESSAGE + "\n")
                self.assertNotIn(secret, error)
                getattr(self.api, name).side_effect = None

    def test_summary_failure_or_nonfinite_output_does_not_emit_partial_json(self):
        self.api.packet_summary.side_effect = ValueError("private source value")
        self.assertEqual(self.invoke(self.arguments), (2, "", feature_cli.FAILURE_MESSAGE + "\n"))
        self.api.packet_summary.side_effect = None
        self.api.packet_summary.return_value = {"bad": float("nan")}
        self.assertEqual(self.invoke(self.arguments), (2, "", feature_cli.FAILURE_MESSAGE + "\n"))

    def test_unknown_or_missing_packet_status_fails_before_printing_a_summary(self):
        for value in ({}, {"status": "APPROVED"}, None):
            with self.subTest(value=value):
                self.api.build_run.return_value = value
                self.assertEqual(self.invoke(self.arguments), (2, "", feature_cli.FAILURE_MESSAGE + "\n"))
        self.api.packet_summary.assert_not_called()

    def test_naive_date_only_and_excess_precision_cutoffs_fail_before_build(self):
        for cutoff in ("2026-09-08", "2026-09-08T12:00:00", "2026-09-08T12:00:00.1234567Z"):
            with self.subTest(cutoff=cutoff):
                arguments = list(self.arguments)
                arguments[arguments.index("--as-of") + 1] = cutoff
                result, output, error = self.invoke(arguments)
                self.assertEqual((result, output), (2, ""))
                self.assertEqual(error, feature_cli.FAILURE_MESSAGE + "\n")
        self.api.build_run.assert_not_called()

    def test_required_arguments_and_subcommand_are_not_inferred(self):
        cases = [[], ["build"], ["show"]]
        for flag in ("--catalog-run-dir", "--as-of", "--run-dir"):
            args = list(self.arguments)
            index = args.index(flag)
            del args[index:index + 2]
            cases.append(args)
        for args in cases:
            with self.subTest(args=args), self.assertRaises(SystemExit) as caught:
                self.invoke(args)
            self.assertEqual(caught.exception.code, 2)
        self.api.build_run.assert_not_called()
        self.api.read_run.assert_not_called()

    def test_workflow_expansion_flags_and_abbreviations_are_rejected_without_echo(self):
        for flag in ("--fundamental-instrument-id", "--news-entity", "--series", "--model", "--env",
                     "--provider", "--training", "--availability-mode", "--catalog-run"):
            output, error = io.StringIO(), io.StringIO()
            args = [*self.arguments, flag, "private-value-not-for-logs"]
            with (patch.dict(sys.modules, {self.api.__name__: self.api}), redirect_stdout(output), redirect_stderr(error)):
                with self.subTest(flag=flag), self.assertRaises(SystemExit) as caught:
                    feature_cli.main(args)
            self.assertEqual(caught.exception.code, 2)
            self.assertNotIn("private-value-not-for-logs", error.getvalue())
            self.assertEqual(output.getvalue(), "")
        self.api.build_run.assert_not_called()

    def test_show_rejects_database_catalog_and_cutoff_arguments(self):
        for flag in ("--db", "--catalog-run-dir", "--as-of", "--instrument-id"):
            with self.subTest(flag=flag), self.assertRaises(SystemExit) as caught:
                self.invoke(["show", "--run-dir", "artifacts/research-features/archived", flag, "unused"])
            self.assertEqual(caught.exception.code, 2)
        self.api.read_run.assert_not_called()

    def test_help_exits_before_importing_packet_api_or_loading_inputs(self):
        for args in (["--help"], ["build", "--help"], ["show", "--help"]):
            output, error = io.StringIO(), io.StringIO()
            with patch.dict(sys.modules, {self.api.__name__: None}), redirect_stdout(output), redirect_stderr(error):
                with self.subTest(args=args), self.assertRaises(SystemExit) as caught:
                    feature_cli.main(args)
            self.assertEqual(caught.exception.code, 0)
            self.assertIn("usage:", output.getvalue())
            self.assertEqual(error.getvalue(), "")

    def test_fresh_import_and_help_have_no_provider_database_env_or_model_imports(self):
        source = str(Path(feature_cli.__file__).resolve().parents[2])
        script = f"""
import importlib.abc
import sys
sys.path.insert(0, {source!r})
class BlockInputs(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        blocked = ('duckdb', 'dotenv', 'yfinance', 'requests', 'sklearn', 'joblib', 'quantpaper.alpaca_paper',
                   'quantpaper.sources', 'quantpaper.ml', 'quantpaper.research.feature_packet')
        if any(fullname == item or fullname.startswith(item + '.') for item in blocked):
            raise RuntimeError('Unexpected input import')
        return None
sys.meta_path.insert(0, BlockInputs())
from quantpaper.research.feature_cli import main
main(['build', '--help'])
"""
        result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--catalog-run-dir", result.stdout)
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
