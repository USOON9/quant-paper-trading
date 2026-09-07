from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from quantpaper.marketdata.storage import read_report, write_json
from quantpaper.marketdata.windows import plan_windows


class MarketDataWindowTests(unittest.TestCase):
    def test_labor_day_uses_friday_and_crypto_uses_previous_utc_day(self):
        plan = plan_windows(now=datetime(2026, 9, 7, 10, tzinfo=timezone.utc))
        self.assertEqual(plan["stocks"]["session"], "2026-09-04")
        self.assertEqual(plan["crypto"]["session"], "2026-09-06")
        self.assertEqual(plan["stocks"]["entry_at"].isoformat(), "2026-09-04T13:35:00+00:00")
        self.assertEqual(plan["stocks"]["exit_at"].isoformat(), "2026-09-04T19:55:00+00:00")
        self.assertEqual(plan["crypto"]["entry_at"].isoformat(), "2026-09-06T00:35:00+00:00")

    def test_crypto_midnight_before_publication_buffer_uses_older_day(self):
        plan = plan_windows(now=datetime(2026, 9, 7, 0, 15, tzinfo=timezone.utc))
        self.assertEqual(plan["crypto"]["session"], "2026-09-05")

    def test_early_close_uses_exchange_close_not_fixed_utc(self):
        plan = plan_windows(now=datetime(2026, 11, 28, tzinfo=timezone.utc), stock_session="2026-11-27")
        self.assertEqual(plan["stocks"]["exit_at"].isoformat(), "2026-11-27T17:55:00+00:00")

    def test_open_or_holiday_requested_sessions_fail(self):
        now = datetime(2026, 9, 8, 16, tzinfo=timezone.utc)
        for kwargs in ({"stock_session": "2026-09-08"}, {"stock_session": "2026-09-07"},
                       {"crypto_session": "2026-09-08"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                plan_windows(now=now, **kwargs)

    def test_naive_clock_rejected(self):
        with self.assertRaises(ValueError):
            plan_windows(now=datetime(2026, 9, 7))


class MarketDataStorageTests(unittest.TestCase):
    def test_create_only_artifacts_and_show_detect_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            run = root / "artifacts/marketdata/run"
            run.mkdir(parents=True)
            plan_hash = write_json(run / "plan.json", {"execution_enabled": False})
            report_hash = write_json(run / "report.json", {"research_only": True})
            with self.assertRaises(FileExistsError):
                write_json(run / "report.json", {"research_only": False})
            write_json(run / "completion.json", {"status": "complete", "artifacts": {
                "plan.json": plan_hash, "report.json": report_hash,
            }})
            self.assertEqual(read_report(run, root), {"research_only": True})
            (run / "report.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "integrity"):
                read_report(run, root)

    def test_completion_cannot_reference_parent_or_absolute_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            run = root / "artifacts/marketdata/run"
            run.mkdir(parents=True)
            for evil in ("../private.json", "/private.json"):
                # The path guard runs before attempting to read the referenced file.
                (run / "completion.json").write_text(
                    '{"status":"complete","artifacts":{' +
                    f'"{evil}":"x","plan.json":"x","report.json":"x"' + '}}'
                )
                with self.assertRaisesRegex(ValueError, "path"):
                    read_report(run, root)


if __name__ == "__main__":
    unittest.main()
