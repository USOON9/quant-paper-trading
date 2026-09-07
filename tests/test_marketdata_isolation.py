"""Market-data collection must stay outside frozen models and trading state."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import Mock, patch

from quantpaper.marketdata.cli import capture, validate_run_directory
from quantpaper.marketdata.client import MarketDataError


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
GATES = {"ENABLE_ALPACA_PAPER": "NO", "ENABLE_ALPACA_PAPER_ROUND_TRIP": "NO"}


class FakeDataClient:
    def __init__(self) -> None:
        self.calls = []

    def fetch(self, kind, symbol, start, end, *, feed, max_pages=3):
        self.calls.append((kind, symbol, start, end, feed))
        stamp = start.isoformat() if hasattr(start, "isoformat") else str(start)
        ending = end.isoformat() if hasattr(end, "isoformat") else str(end)
        if "bar" in kind:
            records = [{"t": stamp, "o": 100, "h": 101, "l": 99, "c": 100,
                        "v": 1000, "n": 10, "vw": 100}]
        else:
            records = [{"t": stamp, "bp": 100, "ap": 100.01, "bs": 10, "as": 12,
                        "bx": "N", "ax": "N", "c": ["R"], "z": "A"}]
        return {"symbol": symbol, "kind": kind, "feed": feed,
                "start": stamp, "end": ending, "observed_at": NOW.isoformat(),
                "records": records, "pages": 1, "complete": True, "truncation_reason": None}


class MarketDataIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name).resolve() / "project"
        self.project.mkdir()
        self.output_base = self.project / "artifacts" / "marketdata"
        self.output_base.mkdir(parents=True)
        self.run_dir = self.output_base / "capture-001"
        self.env_path = self.project / ".env"
        self.env_path.write_text(
            "ENABLE_ALPACA_PAPER=NO\nENABLE_ALPACA_PAPER_ROUND_TRIP=NO\n"
            "APCA_API_KEY_ID=synthetic-private-key-sentinel\n"
            "APCA_API_SECRET_KEY=synthetic-private-secret-sentinel\n"
        )
        self.protected = [self.env_path]
        for name in (
            "artifacts/yahoo_walkforward_v2_model.joblib",
            "artifacts/yahoo_walkforward_v2_model.json",
            "artifacts/yahoo_walkforward_v2_oos.csv",
            "artifacts/shadow-report.json",
            "artifacts/research-v3/frozen-run/manifest.json",
            "artifacts/research-v3/frozen-run/report.json",
            "artifacts/research-v3/frozen-run/predictions.csv",
            "artifacts/research-v3/frozen-run/completion.json",
            "data/research.duckdb",
            "data/shadow/yahoo/SPY.csv",
            "data/yahoo/SPY.csv",
            "automation-fixture.toml",
        ):
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"immutable previous-stage evidence\n")
            self.protected.append(path)

    def hashes(self) -> dict[str, str]:
        return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.protected}

    def collect(self, client):
        return capture(self.run_dir, project_root=self.project, client=client, now=NOW,
                       stock_session="2026-09-04", crypto_session="2026-09-06",
                       symbols=("SPY", "BTC/USD"))

    def test_new_child_run_is_allowed_and_existing_evidence_is_preserved(self) -> None:
        validate_run_directory(self.run_dir, self.project)
        self.run_dir.mkdir()
        report = self.run_dir / "report.json"
        report.write_bytes(b"existing capture evidence")
        with self.assertRaises(ValueError):
            validate_run_directory(self.run_dir, self.project)
        self.assertEqual(report.read_bytes(), b"existing capture evidence")

    def test_output_roots_traversal_and_frozen_directories_are_rejected(self) -> None:
        for candidate in (
            self.project, self.project / "artifacts", self.output_base,
            self.project / "artifacts/research-v3/new-run",
            self.output_base / ".." / "yahoo_walkforward_v2_model.joblib",
            self.project / "data/research.duckdb",
            Path(self.temporary.name).resolve() / "outside-project",
        ):
            with self.subTest(path=str(candidate)):
                with self.assertRaises(ValueError):
                    validate_run_directory(candidate, self.project)

    def test_symlink_escape_and_internal_symlink_ancestors_are_rejected(self) -> None:
        for name, target in (("data-alias", self.project / "data"),
                             ("internal-alias", self.output_base)):
            alias = self.output_base / name
            alias.symlink_to(target, target_is_directory=True)
            with self.subTest(alias=name):
                with self.assertRaises(ValueError):
                    validate_run_directory(alias / "new-run", self.project)
                self.assertFalse((target / "new-run").exists())

    def test_effective_open_environment_gate_blocks_before_client_access(self) -> None:
        client = Mock()
        before = self.hashes()
        for name in GATES:
            with self.subTest(gate=name), patch.dict(os.environ, {**GATES, name: "YES"}):
                with self.assertRaises(RuntimeError):
                    self.collect(client)
        client.fetch.assert_not_called()
        self.assertFalse(self.run_dir.exists())
        self.assertEqual(self.hashes(), before)

    def test_open_file_gate_blocks_client_construction_and_collection(self) -> None:
        self.env_path.write_text("ENABLE_ALPACA_PAPER=YES_I_UNDERSTAND\nENABLE_ALPACA_PAPER_ROUND_TRIP=NO\n")
        with patch.dict(os.environ, {}, clear=True):
            with patch("quantpaper.marketdata.cli.MarketDataClient.from_env") as constructor:
                with self.assertRaises(RuntimeError):
                    self.collect(None)
                constructor.assert_not_called()
        self.assertFalse(self.run_dir.exists())

    def test_fake_capture_preserves_frozen_artifacts_gates_and_credentials(self) -> None:
        client = FakeDataClient()
        before = self.hashes()
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, GATES):
            environment = dict(os.environ)
            with redirect_stdout(out), redirect_stderr(err):
                self.collect(client)
            self.assertEqual(dict(os.environ), environment)
        self.assertEqual(self.hashes(), before)
        self.assertGreater(len(client.calls), 0)
        self.assertEqual({call[4] for call in client.calls if call[1] == "SPY"}, {"sip"})
        self.assertEqual({call[4] for call in client.calls if call[1] == "BTC/USD"}, {"crypto_us"})
        output_files = [path for path in self.run_dir.rglob("*") if path.is_file()]
        self.assertTrue(output_files)
        for path in output_files:
            self.assertFalse(path.is_symlink())
            self.assertNotEqual(path.suffix, ".joblib")
        visible = out.getvalue().encode() + err.getvalue().encode() + b"".join(path.read_bytes() for path in output_files)
        self.assertNotIn(b"synthetic-private-key-sentinel", visible)
        self.assertNotIn(b"synthetic-private-secret-sentinel", visible)

    def test_sip_permission_failure_is_reported_without_feed_fallback(self) -> None:
        class PermissionDeniedClient(FakeDataClient):
            def fetch(self, kind, symbol, start, end, *, feed, max_pages=3):
                if symbol == "SPY":
                    self.calls.append((kind, symbol, start, end, feed))
                    raise MarketDataError("permission", "Requested feed permission denied", http_status=403)
                return super().fetch(kind, symbol, start, end, feed=feed, max_pages=max_pages)

        client = PermissionDeniedClient()
        before = self.hashes()
        with patch.dict(os.environ, GATES), redirect_stderr(io.StringIO()):
            report = self.collect(client)
        self.assertEqual(self.hashes(), before)
        self.assertEqual({call[4] for call in client.calls if call[1] == "SPY"}, {"sip"})
        self.assertEqual(report["status"], "partial")
        self.assertTrue(report["errors"])
        self.assertTrue(all(error["category"] == "permission" for error in report["errors"]))

    def test_page_capped_fetch_reports_partial_without_http_error(self) -> None:
        class PageCappedClient(FakeDataClient):
            def fetch(self, kind, symbol, start, end, *, feed, max_pages=3):
                payload = super().fetch(kind, symbol, start, end, feed=feed, max_pages=max_pages)
                if kind == "quotes":
                    payload["complete"] = False
                    payload["truncation_reason"] = "max_pages_reached"
                return payload

        with patch.dict(os.environ, GATES), redirect_stderr(io.StringIO()):
            report = self.collect(PageCappedClient())
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["status"], "partial")


class MarketDataImportIsolationTests(unittest.TestCase):
    def test_import_and_help_never_load_trading_or_shadow_modules(self) -> None:
        script = textwrap.dedent("""
            import importlib.abc
            import os
            import socket
            import sys

            forbidden = ("alpaca.trading", "quantpaper.alpaca", "quantpaper.alpaca_cli",
                         "quantpaper.alpaca_paper", "quantpaper.shadow", "quantpaper.shadow_cli",
                         "quantpaper.warehouse", "duckdb")
            class ForbidTradingImports(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if any(fullname == name or fullname.startswith(name + ".") for name in forbidden):
                        raise AssertionError("marketdata imported forbidden module: " + fullname)
                    return None
            def no_network(*args, **kwargs):
                raise AssertionError("marketdata import/help attempted network access")
            sys.meta_path.insert(0, ForbidTradingImports())
            socket.create_connection = no_network
            socket.socket.connect = no_network
            before = dict(os.environ)
            from quantpaper.marketdata.cli import main
            try:
                main(["--help"])
            except SystemExit as error:
                if error.code != 0:
                    raise
            else:
                raise AssertionError("help did not exit through argparse")
            if dict(os.environ) != before:
                raise AssertionError("marketdata import/help mutated the environment")
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


if __name__ == "__main__":
    unittest.main()
