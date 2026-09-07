from __future__ import annotations

from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
import json
from unittest.mock import patch

try:
    import pandas as pd

    from quantpaper.shadow import ShadowJournal, ShadowSignal
    from quantpaper.scheduler import exclusive_cycle_lock
    from quantpaper.shadow_cli import main as shadow_main
    from quantpaper.sources.common import canonical_hash
except ImportError:
    pd = None


@unittest.skipIf(pd is None, "shadow dependencies are unavailable")
class ShadowTests(unittest.TestCase):
    def make_signal(self) -> ShadowSignal:
        return ShadowSignal(
            signal_id="fixed-id",
            generated_at=datetime(2026, 9, 4, 21, tzinfo=timezone.utc).isoformat(),
            generation_date="2026-09-04",
            symbol="SPY",
            asset_class="equity",
            feature_as_of="2026-09-04",
            probability_up=0.6,
            direction=1,
            model_hash="a" * 64,
            model_trained_through="2026-09-03",
            model_approved=False,
            feature_hash=canonical_hash({}),
            cost_bps=5.0,
            target_session="2026-09-08",
            target_open="2026-09-08T13:30:00Z",
            target_close="2026-09-08T20:00:00Z",
            feature_payload_json="{}",
        )

    def test_journal_is_idempotent_and_has_no_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = ShadowJournal(Path(directory) / "test.duckdb")
            try:
                signal = self.make_signal()
                self.assertEqual(journal.append([signal]), 1)
                self.assertEqual(journal.append([signal]), 0)
                self.assertFalse(journal.report()["execution_enabled"])
            finally:
                journal.close()

    def test_settlement_uses_exact_holiday_aware_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = ShadowJournal(Path(directory) / "test.duckdb")
            try:
                journal.append([self.make_signal()])
                index = pd.to_datetime(["2026-09-05", "2026-09-08"], utc=True)
                frame = pd.DataFrame(
                    {
                        "open": [100.0, 100.0],
                        "high": [102.0, 103.0],
                        "low": [99.0, 99.0],
                        "close": [102.0, 101.0],
                        "volume": [1.0, 1.0],
                    },
                    index=index,
                )
                self.assertEqual(journal.settle({"SPY": frame}, now="2026-09-08T21:00:00Z"), 1)
                recent = journal.report()["recent"][0]
                self.assertEqual(recent["target_session"], "2026-09-08")
                self.assertAlmostEqual(recent["strategy_return"], 0.0095)
            finally:
                journal.close()

    def test_cannot_settle_unclosed_or_missing_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = ShadowJournal(Path(directory) / "test.duckdb")
            try:
                journal.append([self.make_signal()])
                frame = pd.DataFrame({"open":[100],"high":[103],"low":[99],"close":[101]},
                                     index=pd.to_datetime(["2026-09-08"],utc=True))
                self.assertEqual(journal.settle({"SPY":frame},now="2026-09-08T19:00:00Z"),0)
                frame.index = pd.to_datetime(["2026-09-09"],utc=True)
                self.assertEqual(journal.settle({"SPY":frame},now="2026-09-10T10:00:00Z"),0)
                self.assertEqual(journal.report()["counts"], {"PENDING":1})
            finally:
                journal.close()

    def test_duplicate_target_is_not_added_on_different_generation_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = ShadowJournal(Path(directory) / "test.duckdb")
            try:
                s = self.make_signal()
                self.assertEqual(journal.append([s]),1)
                duplicate = replace(s, signal_id="another", generated_at="2026-09-05T12:00:00Z",
                                    generation_date="2026-09-05")
                self.assertEqual(journal.append([duplicate]),0)
                self.assertEqual(journal.report()["recent"][0]["generated_at"].startswith("2026-09-04"), True)
            finally:
                journal.close()

    def test_late_signal_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = ShadowJournal(Path(directory) / "test.duckdb")
            try:
                with self.assertRaisesRegex(ValueError,"before target open"):
                    journal.append([replace(self.make_signal(),generated_at="2026-09-08T14:00:00Z")])
            finally:
                journal.close()

    def test_transaction_rolls_back_cycle_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = ShadowJournal(Path(directory) / "test.duckdb")
            try:
                with self.assertRaises(RuntimeError):
                    with journal.transaction():
                        journal.append([self.make_signal()])
                        raise RuntimeError("simulated cycle interruption")
                self.assertEqual(journal.report()["counts"],{})
            finally:
                journal.close()

    def test_lock_precedes_database_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "test.duckdb"
            with exclusive_cycle_lock(db.with_suffix(".shadow.lock")):
                with patch("quantpaper.shadow_cli.ShadowJournal") as journal:
                    self.assertEqual(shadow_main(["report","--database",str(db)]),2)
                    journal.assert_not_called()

    def test_legacy_signals_are_preserved_but_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "test.duckdb"
            journal = ShadowJournal(db)
            journal.append([self.make_signal()])
            journal.connection.execute("UPDATE shadow_signals SET feature_contract_version=NULL")
            journal.close()
            journal = ShadowJournal(db)
            try:
                self.assertEqual(journal.report()["counts"],{"INVALIDATED_LEGACY":1})
                self.assertEqual(journal.connection.execute("SELECT legacy_status FROM shadow_signals").fetchone()[0],"PENDING")
                self.assertEqual(journal.report()["evaluated"]["observations"],0)
            finally:
                journal.close()


if __name__ == "__main__":
    unittest.main()
