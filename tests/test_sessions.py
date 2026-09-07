"""Calendar filtering must retain valid bars outside the default time window."""

import unittest

try:
    import pandas as pd
    from quantpaper.sessions import completed_bars
except ImportError:
    pd = None


@unittest.skipIf(pd is None, "calendar dependencies are unavailable")
class SessionCoverageTests(unittest.TestCase):
    def test_max_history_keeps_1993_sessions_and_current_close_buffer(self):
        frame = pd.DataFrame({"close": [43.94, 44.0, 44.25, 100.0]},
                             index=pd.to_datetime(["1993-01-29", "1993-01-30", "1993-02-01", "2026-09-04"], utc=True))
        before = completed_bars(frame, "SPY", "2026-09-04T20:29:00Z")
        self.assertEqual(before.index.strftime("%Y-%m-%d").tolist(), ["1993-01-29", "1993-02-01"])
        after = completed_bars(frame, "SPY", "2026-09-04T20:31:00Z")
        self.assertEqual(after.index.strftime("%Y-%m-%d").tolist(), ["1993-01-29", "1993-02-01", "2026-09-04"])

    def test_weekend_only_input_is_filtered_without_calendar_construction_error(self):
        frame = pd.DataFrame({"close": [100.0]}, index=pd.to_datetime(["1993-01-30"], utc=True))
        self.assertTrue(completed_bars(frame, "SPY", "2026-09-05T12:00:00Z").empty)

    def test_invalid_date_cannot_be_silently_filtered(self):
        frame = pd.DataFrame({"close": [100.0]}, index=pd.DatetimeIndex([pd.NaT], tz="UTC"))
        with self.assertRaisesRegex(ValueError, "invalid daily bar dates"):
            completed_bars(frame, "SPY", "2026-09-05T12:00:00Z")


if __name__ == "__main__":
    unittest.main()
