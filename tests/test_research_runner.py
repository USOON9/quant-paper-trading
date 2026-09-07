from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from quantpaper.research.cli import read_report, run_research


REAL_ROOT = Path(__file__).resolve().parents[1]


class ResearchRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "project"
        self.root.mkdir()
        self.data = self.root / "data/yahoo"
        self.data.mkdir(parents=True)
        self.protocol = self.root / "configs/research_v3.toml"
        self.protocol.parent.mkdir()
        self.protocol.write_bytes((REAL_ROOT / "configs/research_v3.toml").read_bytes())
        (self.root / ".env").write_text("ENABLE_ALPACA_PAPER=NO\nENABLE_ALPACA_PAPER_ROUND_TRIP=NO\n")
        for name in ("main.py", "pyproject.toml", "requirements-tested.txt"):
            (self.root / name).write_text("fixture code metadata\n")
        for symbol in ("SPY", "JPM", "XOM", "WMT", "JNJ", "BTC-USD", "QQQ", "IWM", "INDEX_VIX", "INDEX_TNX"):
            (self.data / f"{symbol}.csv").write_text(
                "timestamp,open,high,low,close,volume\n"
                "2026-09-03T00:00:00Z,100,102,99,101,1000\n"
                "2026-09-04T00:00:00Z,101,103,100,102,1200\n"
            )
        self.protected = [self.root / ".env", self.root / "data/research.duckdb"]
        for name in ("yahoo_walkforward_v2_model.joblib", "yahoo_walkforward_v2_model.json",
                     "yahoo_walkforward_v2_oos.csv", "shadow-report.json"):
            self.protected.append(self.root / "artifacts" / name)
        self.protected.append(self.root / "automation-fixture.toml")
        for path in self.protected[1:]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"immutable sentinel")
        self.protected.extend(self.data.glob("*.csv"))
        self.run_dir = self.root / "artifacts/research-v3/run-001"

    def hashes(self):
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.protected}

    def build(self, frame, symbol, context):
        self.assertEqual(frame.index.max().date().isoformat(), "2026-09-03")
        return pd.DataFrame({"symbol": [symbol]}, index=frame.index[:1])

    def evaluate(self, frame, group, protocol):
        return ({"symbols": group["symbols"], "selected_cost_bps": group["selected_cost_bps"],
                 "models": {}, "oos_start": "2026-09-03", "oos_end": "2026-09-03"}, frame)

    def run_mocked(self):
        with patch("quantpaper.research.cli.build_timing_dataset", side_effect=self.build), \
             patch("quantpaper.research.cli.evaluate_group", side_effect=self.evaluate):
            return run_research(self.protocol, self.data, self.run_dir, project_root=self.root)

    def test_complete_run_preserves_inputs_v2_shadow_and_gates(self):
        before = self.hashes()
        report = self.run_mocked()
        self.assertEqual(before, self.hashes())
        self.assertFalse(report["execution_enabled"])
        self.assertFalse(report["approved_for_paper_signals"])
        self.assertFalse(report["deployment_artifact_created"])
        self.assertEqual(set(p.name for p in self.run_dir.iterdir()),
                         {"manifest.json", "predictions.csv", "report.json", "completion.json"})
        self.assertEqual(read_report(self.run_dir, self.root), report)

    def test_same_run_cannot_overwrite_report(self):
        self.run_mocked()
        before = (self.run_dir / "report.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.run_mocked()
        self.assertEqual(before, (self.run_dir / "report.json").read_bytes())

    def test_mutated_input_during_fit_never_publishes_completion(self):
        def changing_fit(frame, group, protocol):
            path = self.data / "SPY.csv"
            path.write_bytes(path.read_bytes() + b"\n")
            return self.evaluate(frame, group, protocol)
        with patch("quantpaper.research.cli.build_timing_dataset", side_effect=self.build), \
             patch("quantpaper.research.cli.evaluate_group", side_effect=changing_fit):
            with self.assertRaisesRegex(ValueError, "snapshot changed"):
                run_research(self.protocol, self.data, self.run_dir, project_root=self.root)
        self.assertFalse((self.run_dir / "completion.json").exists())
        self.assertFalse((self.run_dir / "report.json").exists())

    def test_output_tamper_is_detected_by_show(self):
        self.run_mocked()
        (self.run_dir / "report.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "integrity"):
            read_report(self.run_dir, self.root)


if __name__ == "__main__":
    unittest.main()
