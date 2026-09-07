from __future__ import annotations

from datetime import datetime, timezone
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd
from pandas.testing import assert_frame_equal

from quantpaper.sources.yahoo_snapshot import YahooSnapshotClient


class YahooSnapshotTests(unittest.TestCase):
    def frame(self, dates=None):
        dates = ["2026-09-03T04:00:00Z", "2026-09-04T04:00:00Z"] if dates is None else dates
        return pd.DataFrame(
            {"Open": [10.0] * len(dates), "High": [12.0] * len(dates),
             "Low": [9.0] * len(dates), "Close": [11.0] * len(dates),
             "Volume": [100] * len(dates)}, index=pd.Index(dates),
        )

    def fetch(self, frame=None, now="2026-09-07T13:00:00Z", **kwargs):
        source = self.frame() if frame is None else frame
        return YahooSnapshotClient(
            get_history=lambda symbol, **options: source,
            now=lambda: pd.Timestamp(now),
        ).fetch("JPM", **kwargs)

    def test_provider_options_and_clock_order(self):
        events = []
        captured = {}

        def provider(symbol, **options):
            events.append("provider")
            captured.update(symbol=symbol, **options)
            return self.frame()

        def clock():
            events.append("clock")
            return datetime(2026, 9, 7, 13, tzinfo=timezone.utc)

        result = YahooSnapshotClient(provider, clock).fetch(" jpm ")
        self.assertEqual(events, ["provider", "clock"])
        self.assertEqual(captured["symbol"], "JPM")
        self.assertEqual(captured["period"], "2y")
        for key in ("repair", "actions", "prepost", "back_adjust", "rounding"):
            self.assertIs(captured[key], False)
        for key in ("auto_adjust", "keepna", "raise_errors"):
            self.assertIs(captured[key], True)
        self.assertEqual(captured["interval"], "1d")
        self.assertEqual(captured["timeout"], 30)
        self.assertEqual(result.observed_at, "2026-09-07T13:00:00Z")

    def test_normalized_copy_sorted_utc(self):
        source = self.frame(["2026-09-04T00:00:00-04:00", "2026-09-03T00:00:00-04:00"])
        original = source.copy(deep=True)
        result = self.fetch(source)
        assert_frame_equal(source, original)
        self.assertEqual(list(result.frame.columns), ["open", "high", "low", "close", "volume"])
        self.assertEqual(str(result.frame.index.tz), "UTC")
        self.assertEqual(result.frame.index.name, "timestamp")
        self.assertTrue(result.frame.index.is_monotonic_increasing)
        self.assertEqual(result.excluded_incomplete_rows, 0)

    def test_hash_deterministic_independent_of_input_order(self):
        source = self.frame()
        a = self.fetch(source)
        b = self.fetch(source.iloc[::-1])
        self.assertEqual(a.content_hash, b.content_hash)
        self.assertEqual(len(a.content_hash), 64)
        changed = source.copy()
        changed.loc[changed.index[0], "Volume"] = 200
        self.assertNotEqual(a.content_hash, self.fetch(changed).content_hash)
        self.assertNotEqual(a.content_hash, self.fetch(source, period="1y").content_hash)
        self.assertNotEqual(a.content_hash, self.fetch(source, now="2026-09-07T14:00:00Z").content_hash)

    def test_hash_canonical_content(self):
        result = self.fetch()
        rows = [{"timestamp": timestamp.isoformat().replace("+00:00", "Z"), **row}
                for timestamp, row in zip(result.frame.index, result.frame.to_dict("records"), strict=True)]
        payload = {"request": result.request, "observed_at": result.observed_at,
                   "rows": rows, "excluded_incomplete_rows": result.excluded_incomplete_rows}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        self.assertEqual(result.content_hash, digest)

    def test_completion_exact_cutoff_and_one_nanosecond_before(self):
        source = self.frame(["2026-09-04T04:00:00Z"])
        self.assertEqual(len(self.fetch(source, now="2026-09-05T04:00:00Z").frame), 1)
        result = self.fetch(source, now="2026-09-05T03:59:59.999999999Z")
        self.assertTrue(result.frame.empty)
        self.assertEqual(result.excluded_incomplete_rows, 1)

    def test_incomplete_bars_excluded_and_counted(self):
        source = self.frame(["2026-09-04T04:00:00Z", "2026-09-07T04:00:00Z"])
        result = self.fetch(source)
        self.assertEqual(len(result.frame), 1)
        self.assertEqual(result.excluded_incomplete_rows, 1)

    def test_invalid_incomplete_bar_is_not_hidden(self):
        source = self.frame(["2026-09-07T04:00:00Z"])
        source.loc[source.index[0], "High"] = float("nan")
        with self.assertRaisesRegex(ValueError, "nonfinite"):
            self.fetch(source)

    def test_empty_and_non_frame_rejected(self):
        for source in (pd.DataFrame(), [], None):
            with self.subTest(source=type(source).__name__):
                client = YahooSnapshotClient(lambda *args, **kwargs: source)
                with self.assertRaises(ValueError):
                    client.fetch("JPM")

    def test_row_limit_rejected(self):
        source = self.frame(pd.date_range("2020-01-01", periods=1501, tz="UTC"))
        with self.assertRaisesRegex(ValueError, "row limit"):
            self.fetch(source)

    def test_unsafe_symbols_rejected_before_provider(self):
        def provider(*args, **kwargs):
            self.fail("Provider must not be called")

        client = YahooSnapshotClient(provider)
        for symbol in (None, "", " ", "../JPM", "JPM/USD", "JPM\\x", "^GSPC", "JPM?x", "x;ls", "A" * 16):
            with self.subTest(symbol=symbol), self.assertRaisesRegex(ValueError, "symbol"):
                client.fetch(symbol)

    def test_period_allowlist(self):
        for period in ("max", "5y", "1d", "2Y", "", None, []):
            with self.subTest(period=period), self.assertRaisesRegex(ValueError, "period"):
                self.fetch(period=period)
        self.assertEqual(self.fetch(period="1y").request["period"], "1y")

    def test_missing_duplicate_and_multiindex_columns(self):
        missing = self.frame().drop(columns="Volume")
        duplicate = self.frame()
        duplicate[" open "] = 10
        multi = self.frame()
        multi.columns = pd.MultiIndex.from_product([list(multi.columns), ["JPM"]])
        for source in (missing, duplicate, multi):
            with self.assertRaisesRegex(ValueError, "columns"):
                self.fetch(source)

    def test_naive_missing_malformed_timestamps(self):
        for bad in ("2026-09-03", pd.NaT, None, "secret-invalid-timestamp", 123456789):
            with self.subTest(bad=str(bad)), self.assertRaisesRegex(ValueError, "timestamps") as caught:
                self.fetch(self.frame([bad]))
            self.assertNotIn("secret-invalid", str(caught.exception))

    def test_duplicate_instants_and_daily_dates(self):
        for dates in (["2026-09-03T04:00:00Z", "2026-09-03T00:00:00-04:00"],
                      ["2026-09-03T04:00:00Z", "2026-09-03T05:00:00Z"]):
            with self.assertRaisesRegex(ValueError, "duplicate"):
                self.fetch(self.frame(dates))

    def test_bad_numeric_rows(self):
        for value in (float("nan"), float("inf"), -float("inf"), None, "10", True):
            source = self.frame().astype(object)
            source.loc[source.index[0], "Open"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "nonfinite"):
                self.fetch(source)

    def test_ohlc_and_volume_bounds(self):
        for column, value in (("Open", 0), ("Low", -1), ("Volume", -1), ("High", 10), ("Low", 11.5)):
            source = self.frame()
            source.loc[source.index[0], column] = value
            with self.subTest(column=column, value=value), self.assertRaises(ValueError):
                self.fetch(source)

    def test_zero_volume_is_valid(self):
        source = self.frame()
        source["Volume"] = 0
        self.assertEqual(self.fetch(source).frame["volume"].sum(), 0)

    def test_observation_clock_must_be_aware(self):
        for now in ("2026-09-07T13:00:00", "NaT"):
            with self.assertRaisesRegex(ValueError, "clock"):
                self.fetch(now=now)

    def test_provider_exception_sanitized(self):
        def provider(*args, **kwargs):
            raise RuntimeError("https://example.test/?cookie=secret")

        with self.assertRaisesRegex(ValueError, "request failed") as caught:
            YahooSnapshotClient(provider).fetch("JPM")
        self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_module_import_does_not_import_provider_or_warehouse(self):
        command = "import sys; import quantpaper.sources.yahoo_snapshot; assert not any(x in sys.modules for x in ('yfinance','dotenv','alpaca','quantpaper.warehouse'))"
        completed = subprocess.run([sys.executable, "-c", command], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_default_provider_uses_and_cleans_isolated_cache(self):
        events = []
        paths = []

        def set_location(path):
            self.assertTrue(Path(path).is_dir())
            paths.append(Path(path))
            events.append("cache")

        def history(**kwargs):
            events.append("history")
            print("private provider diagnostic")
            print("private provider error", file=sys.stderr)
            return self.frame()

        def ticker(symbol):
            self.assertEqual(events, ["cache"])
            self.assertEqual(symbol, "JPM")
            events.append("ticker")
            return SimpleNamespace(history=history)

        fake = SimpleNamespace(set_tz_cache_location=set_location, Ticker=ticker)
        output = io.StringIO()
        with patch.dict(sys.modules, {"yfinance": fake}), redirect_stdout(output), redirect_stderr(output):
            result = YahooSnapshotClient(now=lambda: pd.Timestamp("2026-09-07T13:00:00Z")).fetch("JPM")
        self.assertEqual(events, ["cache", "ticker", "history", "cache"])
        self.assertEqual(paths[0], paths[1])
        self.assertFalse(paths[0].exists())
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(len(result.frame), 2)

    def test_default_provider_failure_still_cleans_cache(self):
        paths = []

        def fail(**kwargs):
            raise RuntimeError("private provider details")

        fake = SimpleNamespace(
            set_tz_cache_location=lambda path: paths.append(Path(path)),
            Ticker=lambda symbol: SimpleNamespace(history=fail),
        )
        with patch.dict(sys.modules, {"yfinance": fake}), self.assertRaisesRegex(ValueError, "request failed"):
            YahooSnapshotClient().fetch("JPM")
        self.assertEqual(len(paths), 2)
        self.assertFalse(paths[0].exists())


if __name__ == "__main__":
    unittest.main()
