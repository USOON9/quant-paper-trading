from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

try:
    from quantpaper.source_records import FundamentalRecord, MacroRecord
    from quantpaper.warehouse import PointInTimeWarehouse
except ImportError:
    PointInTimeWarehouse = None


@unittest.skipIf(PointInTimeWarehouse is None, "data optional dependencies are not installed")
class WarehouseTests(unittest.TestCase):
    def test_schema_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            warehouse = PointInTimeWarehouse(Path(directory) / "test.duckdb")
            try:
                warehouse.initialize()
                warehouse.initialize()
                stats = warehouse.stats()
                self.assertEqual(stats.market_bars, 0)
                self.assertEqual(stats.fundamentals, 0)
                self.assertEqual(stats.instrument_aliases, 0)
                self.assertEqual(stats.ingestion_runs, 0)
            finally:
                warehouse.close()


@unittest.skipIf(PointInTimeWarehouse is None, "data optional dependencies are not installed")
class WarehousePointInTimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.warehouse = PointInTimeWarehouse(Path(self.directory.name) / "test.duckdb")

    def tearDown(self) -> None:
        self.warehouse.close()
        self.directory.cleanup()

    def fact(self, **kwargs):
        original = FundamentalRecord(
            "US:TEST", "Revenues", "2024-06-30", 10.0, "USD", "10-Q", "first",
            "2024-08-02T04:00:00Z", "2024-08-02T04:00:00Z", period_start="2024-04-01",
        )
        return replace(original, **kwargs)

    def test_fundamentals_as_of_preserves_periods_and_selects_known_amendment(self):
        records = [self.fact(), self.fact(period_start="2024-01-01", value=20),
                   self.fact(unit="EUR", value=9),
                   self.fact(accession_number="later", value=11, form="10-Q/A",
                             filed_at="2024-08-11T04:00:00Z", available_at="2024-08-11T04:00:00Z")]
        self.assertEqual(self.warehouse.ingest_fundamentals(records), 4)
        self.assertEqual(self.warehouse.ingest_fundamentals(records), 0)
        self.assertTrue(self.warehouse.fundamentals_as_of("US:TEST", "2024-08-02T03:59:59Z").empty)
        before = self.warehouse.fundamentals_as_of("US:TEST", "2024-08-03T00:00:00Z")
        after = self.warehouse.fundamentals_as_of("US:TEST", "2024-08-12T00:00:00Z")
        self.assertEqual(sorted(before.value), [9, 10, 20])
        self.assertEqual(sorted(after.value), [9, 11, 20])

    def test_conflicting_fact_rolls_back_the_whole_batch(self):
        with self.assertRaisesRegex(ValueError, "changed within"):
            self.warehouse.ingest_fundamentals([self.fact(), self.fact(value=999)])
        self.assertEqual(self.warehouse.stats().fundamentals, 0)

    def test_source_batch_and_audit_are_atomic(self):
        with self.assertRaisesRegex(RuntimeError, "audit failure"):
            with self.warehouse.transaction():
                self.warehouse.ingest_fundamentals([self.fact()])
                self.warehouse.record_ingestion("sec", {}, 1, "hash")
                raise RuntimeError("audit failure")
        self.assertEqual(self.warehouse.stats().fundamentals, 0)
        self.assertEqual(self.warehouse.stats().ingestion_runs, 0)

    def test_macro_as_of_does_not_use_future_revision(self):
        original = MacroRecord("TEST", "2024-01-01", 3, "2024-02-01",
                               "2024-02-02T06:00:00Z", realtime_end="2024-02-29")
        revision = replace(original, value=3.1, vintage_date="2024-03-01",
                           available_at="2024-03-02T06:00:00Z", realtime_end="9999-12-31")
        self.warehouse.ingest_macro([original, revision])
        # Even after an old interval expired at the provider, our conservative
        # availability cutoff must not expose the new value hours early.
        before = self.warehouse.macro_as_of("TEST", "2024-03-02T01:00:00Z")
        after = self.warehouse.macro_as_of("TEST", "2024-03-02T06:00:00Z")
        self.assertEqual(before.value.tolist(), [3])
        self.assertEqual(after.value.tolist(), [3.1])

    def test_v3_migration_preserves_ambiguous_rows_separately(self):
        db = self.warehouse.connection
        db.execute("CREATE TABLE fundamentals (metric VARCHAR, value DOUBLE)")
        db.execute("INSERT INTO fundamentals VALUES ('Revenues', 42)")
        db.execute("CREATE TABLE macro_observations (series_id VARCHAR, value DOUBLE)")
        db.execute("INSERT INTO macro_observations VALUES ('TEST', 1)")
        self.warehouse.initialize()
        self.warehouse.initialize()
        self.assertEqual(db.execute("SELECT value FROM fundamentals_legacy_v3").fetchone()[0], 42)
        self.assertEqual(db.execute("SELECT value FROM macro_observations_legacy_v3").fetchone()[0], 1)
        self.assertEqual(self.warehouse.stats().fundamentals, 0)
        self.assertEqual(self.warehouse.quality_report()["excluded_from_as_of"]["fundamentals_legacy_v3"], 1)

    def test_adjusted_yahoo_revisions_are_only_available_after_observation(self):
        path = Path(self.directory.name) / "TEST.csv"
        header = "timestamp,open,high,low,close,volume\n"
        path.write_text(header + "2024-01-02,10,12,9,11,100\n")
        with patch("quantpaper.warehouse.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2024, 2, 1, tzinfo=timezone.utc)
            self.assertEqual(self.warehouse.ingest_yahoo_csv("TEST", path, "equity"), 1)
            self.assertEqual(self.warehouse.ingest_yahoo_csv("TEST", path, "equity"), 0)
        self.assertTrue(self.warehouse.bars_as_of("YF:TEST", "2024-01-31T23:59:59Z").empty)
        path.write_text(header + "2024-01-02,5,6,4.5,5.5,100\n")
        with patch("quantpaper.warehouse.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2024, 3, 1, tzinfo=timezone.utc)
            self.assertEqual(self.warehouse.ingest_yahoo_csv("TEST", path, "equity"), 1)
        self.assertEqual(self.warehouse.bars_as_of("YF:TEST", "2024-02-02T00:00:00Z").close.tolist(), [11])
        self.assertEqual(self.warehouse.bars_as_of("YF:TEST", "2024-03-02T00:00:00Z").close.tolist(), [5.5])
        # A provider can undo a correction. Equal values to an old snapshot
        # must still supersede the immediately preceding, different revision.
        path.write_text(header + "2024-01-02,10,12,9,11,100\n")
        with patch("quantpaper.warehouse.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2024, 4, 1, tzinfo=timezone.utc)
            self.assertEqual(self.warehouse.ingest_yahoo_csv("TEST", path, "equity"), 1)
        self.assertEqual(self.warehouse.bars_as_of("YF:TEST", "2024-04-02T00:00:00Z").close.tolist(), [11])

    def test_invalid_yahoo_batch_cannot_leave_an_instrument(self):
        path = Path(self.directory.name) / "TEST.csv"
        path.write_text("timestamp,open,high,low,close,volume\n2024-01-02,10,8,9,11,100\n")
        with self.assertRaisesRegex(ValueError, "OHLC"):
            self.warehouse.ingest_yahoo_csv("TEST", path, "equity")
        self.assertEqual(self.warehouse.stats().instruments, 0)

    def test_macro_vintages_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            warehouse = PointInTimeWarehouse(Path(directory) / "research.duckdb")
            try:
                record = MacroRecord(
                    "CPIAUCSL", "2024-01-01", 310.1, "2024-02-13", "2024-02-14T00:00:00Z"
                )
                self.assertEqual(warehouse.ingest_macro([record]), 1)
                self.assertEqual(warehouse.ingest_macro([record]), 0)
                report = warehouse.quality_report()
                self.assertEqual(report["violations"]["macro_available_before_vintage"], 0)
                self.assertEqual(report["violations"]["news_available_before_first_seen"], 0)
            finally:
                warehouse.close()


if __name__ == "__main__":
    unittest.main()
