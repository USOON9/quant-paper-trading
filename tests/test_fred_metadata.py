from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import traceback
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from quantpaper.sources.common import canonical_hash
from quantpaper.sources.fred_metadata import FREDSeriesMetadataClient, validate_metadata_record
from quantpaper.sources.http import SourceHTTPError


class FREDMetadataTests(unittest.TestCase):
    KEY = "a" * 32
    START = datetime(2026, 9, 7, 13, tzinfo=timezone.utc)
    END = datetime(2026, 9, 7, 13, 0, 1, tzinfo=timezone.utc)

    def payload(self):
        return {
            "realtime_start": "2026-09-07", "realtime_end": "2026-09-07",
            "seriess": [{
                "id": "DGS10", "realtime_start": "2026-09-07", "realtime_end": "2026-09-07",
                "title": "Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity",
                "observation_start": "1962-01-02", "observation_end": "2026-09-04",
                "frequency": "Daily", "frequency_short": "D", "units": "Percent",
                "units_short": "%", "seasonal_adjustment": "Not Seasonally Adjusted",
                "seasonal_adjustment_short": "NSA", "last_updated": "2026-09-04 15:16:03-05",
                "notes": "Provider notes must be hashed but never stored in the record.",
                "popularity": 99,
            }],
        }

    def fetch(self, payload=None, clocks=None):
        ticks = iter([self.START, self.END] if clocks is None else clocks)
        provider = Mock(return_value=self.payload() if payload is None else payload)
        client = FREDSeriesMetadataClient(self.KEY, provider, lambda: next(ticks))
        return client.fetch(" dgs10 ")

    def test_current_request_and_observed_availability(self):
        events = []
        ticks = iter([self.START, self.END])

        def clock():
            events.append("clock")
            return next(ticks)

        def provider(url):
            events.append("provider")
            parsed = urlsplit(url)
            self.assertEqual(parsed.scheme, "https")
            self.assertEqual(parsed.netloc, "api.stlouisfed.org")
            self.assertEqual(parsed.path, "/fred/series")
            self.assertEqual(parse_qs(parsed.query), {
                "series_id": ["DGS10"], "file_type": ["json"], "api_key": [self.KEY],
                "realtime_start": ["2026-09-07"], "realtime_end": ["2026-09-07"],
            })
            return self.payload()

        batch = FREDSeriesMetadataClient(self.KEY, provider, clock).fetch(" dgs10 ")
        self.assertEqual(events, ["clock", "provider", "clock"])
        record = batch.records[0]
        self.assertEqual(record["observed_at"], "2026-09-07T13:00:01.000000Z")
        self.assertEqual(record["available_at"], record["observed_at"])
        self.assertEqual(record["last_updated"], "2026-09-04T20:16:03.000000Z")
        self.assertEqual(batch.source, "fred-series-metadata")
        self.assertEqual(record["source_url"], "https://fred.stlouisfed.org/series/DGS10")
        self.assertNotIn("api_key", batch.request)
        self.assertNotIn(self.KEY, json.dumps(batch.request))
        self.assertEqual(validate_metadata_record(record), record)

    def test_full_payload_hash_does_not_persist_arbitrary_fields(self):
        payload = self.payload()
        original = deepcopy(payload)
        batch = self.fetch(payload)
        self.assertEqual(payload, original)
        self.assertEqual(batch.content_hash, canonical_hash(payload))
        self.assertNotIn("notes", batch.records[0])
        self.assertNotIn("popularity", batch.records[0])
        payload["seriess"][0]["notes"] = "Different notes"
        changed = self.fetch(payload)
        self.assertNotEqual(changed.content_hash, batch.content_hash)
        self.assertEqual(changed.records, batch.records)

    def test_cross_midnight_request_retains_start_date(self):
        batch = self.fetch(clocks=[datetime(2026, 9, 7, 23, 59, 59, tzinfo=timezone.utc),
                                  datetime(2026, 9, 8, tzinfo=timezone.utc)])
        self.assertEqual(batch.request["realtime_start"], "2026-09-07")
        self.assertEqual(batch.records[0]["observed_at"], "2026-09-08T00:00:00.000000Z")

    def test_offset_formats_are_normalized(self):
        for value, expected in (
            ("2026-09-04 15:16:03-05", "2026-09-04T20:16:03.000000Z"),
            ("2026-09-04T15:16:03-05:00", "2026-09-04T20:16:03.000000Z"),
            ("2026-09-04T15:16:03-0500", "2026-09-04T20:16:03.000000Z"),
            ("2026-09-04T15:16:03.123456Z", "2026-09-04T15:16:03.123456Z"),
        ):
            payload = self.payload()
            payload["seriess"][0]["last_updated"] = value
            with self.subTest(value=value):
                self.assertEqual(self.fetch(payload).records[0]["last_updated"], expected)

    def test_timestamp_naive_invalid_and_future_rejected(self):
        for value in ("2026-09-04 15:16:03", "2026-09-04", "bad-private-text", None,
                      "2026-09-04T15:16:03+25", "2026-02-30T00:00:00Z", "2026-09-08T00:00:00Z"):
            payload = self.payload()
            payload["seriess"][0]["last_updated"] = value
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                self.fetch(payload)
            self.assertNotIn("bad-private", str(caught.exception))

    def test_invalid_clocks_and_backward_clock(self):
        for clocks in ([datetime(2026, 9, 7), self.END], [self.START, datetime(2026, 9, 7)],
                       [self.END, self.START], [None, self.END]):
            with self.subTest(clocks=clocks), self.assertRaisesRegex(ValueError, "clock"):
                self.fetch(clocks=clocks)

    def test_key_and_series_validation_prevents_requests(self):
        provider = Mock()
        for key in (None, "", "a" * 31, "a" * 33, "A" * 32, "?" * 32):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "FRED_API_KEY"):
                FREDSeriesMetadataClient(key, provider)
        client = FREDSeriesMetadataClient(self.KEY, provider)
        for symbol in (None, "", "DGS10?x=secret", "../DGS10", "A" * 65, "DGS 10"):
            with self.subTest(symbol=symbol), self.assertRaisesRegex(ValueError, "series ID"):
                client.fetch(symbol)
        provider.assert_not_called()

    def test_exact_one_matching_series_required(self):
        for entries in (None, [], {}, [self.payload()["seriess"][0]] * 2, [None]):
            payload = self.payload()
            payload["seriess"] = entries
            with self.subTest(entries=entries), self.assertRaisesRegex(ValueError, "exactly one"):
                self.fetch(payload)
        for symbol in ("DGS1", "dgs10", None):
            payload = self.payload()
            payload["seriess"][0]["id"] = symbol
            with self.subTest(symbol=symbol), self.assertRaisesRegex(ValueError, "match"):
                self.fetch(payload)

    def test_request_envelope_and_record_dates_must_match(self):
        for in_record in (False, True):
            for field in ("realtime_start", "realtime_end"):
                for value in (None, "2026-09-06", "9999-12-31", "20260907"):
                    payload = self.payload()
                    target = payload["seriess"][0] if in_record else payload
                    target[field] = value
                    with self.subTest(in_record=in_record, field=field, value=value), self.assertRaises(ValueError):
                        self.fetch(payload)

    def test_observation_dates_strict_and_ordered(self):
        for field, value in (("observation_start", "19620102"), ("observation_end", "2026-02-30"),
                             ("observation_start", "2026-09-05"), ("observation_end", None)):
            payload = self.payload()
            payload["seriess"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, "date"):
                self.fetch(payload)

    def test_required_text_bounded_nonempty_and_control_free(self):
        for field in ("title", "frequency", "frequency_short", "units", "units_short",
                      "seasonal_adjustment", "seasonal_adjustment_short"):
            for value in (None, "", "  ", 12, "x" * 1025, "private\ntext", "\x00"):
                payload = self.payload()
                payload["seriess"][0][field] = value
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, "text"):
                    self.fetch(payload)

    def test_missing_fields_rejected(self):
        for field in ("title", "frequency", "units", "seasonal_adjustment", "last_updated", "observation_start"):
            payload = self.payload()
            del payload["seriess"][0][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.fetch(payload)

    def test_api_errors_and_non_json_payloads_rejected(self):
        for payload in ([], {"error_code": 400, "error_message": "private URL"}):
            with self.assertRaisesRegex(ValueError, "response"):
                self.fetch(payload)
        for extra in (float("nan"), float("inf"), object(), (1, 2), {1: "bad key"}):
            payload = self.payload()
            payload["unused"] = extra
            with self.subTest(extra=type(extra).__name__), self.assertRaisesRegex(ValueError, "JSON"):
                self.fetch(payload)

    def test_oversized_payload_rejected_even_with_injected_provider(self):
        payload = self.payload()
        payload["seriess"][0]["notes"] = "x" * 1_000_000
        with self.assertRaisesRegex(ValueError, "oversized"):
            self.fetch(payload)

    def test_transport_failure_does_not_leak_key_in_traceback(self):
        def fail(url):
            raise RuntimeError(url)

        try:
            FREDSeriesMetadataClient(self.KEY, fail).fetch("DGS10")
        except RuntimeError:
            rendered = traceback.format_exc()
        else:
            self.fail("Expected failure")
        self.assertNotIn(self.KEY, rendered)
        self.assertNotIn("api_key=", rendered)
        self.assertIn("FRED metadata request failed", rendered)

    def test_safe_transport_category_retained(self):
        provider = Mock(side_effect=SourceHTTPError("tls_verification"))
        with self.assertRaises(SourceHTTPError) as caught:
            FREDSeriesMetadataClient(self.KEY, provider).fetch("DGS10")
        self.assertEqual(caught.exception.category, "tls_verification")

    def test_transport_categories_and_chained_errors_are_sanitized(self):
        for category in ("tls_verification", f"private-api-key-{self.KEY}"):
            def fail(url):
                try:
                    raise RuntimeError(url)
                except RuntimeError as error:
                    raise SourceHTTPError(category) from error

            try:
                FREDSeriesMetadataClient(self.KEY, fail).fetch("DGS10")
            except SourceHTTPError:
                rendered = traceback.format_exc()
            else:
                self.fail("Expected failure")
            self.assertNotIn(self.KEY, rendered)
            self.assertNotIn("api_key=", rendered)

    def test_default_transport_is_fixed_host_and_one_megabyte(self):
        with patch("quantpaper.sources.fred_metadata.bounded_get_json", return_value=self.payload()) as getter:
            ticks = iter([self.START, self.END])
            FREDSeriesMetadataClient(self.KEY, now=lambda: next(ticks)).fetch("DGS10")
        self.assertEqual(getter.call_count, 1)
        self.assertEqual(getter.call_args.kwargs, {
            "allowed_hosts": frozenset({"api.stlouisfed.org"}), "max_bytes": 1_000_000,
        })

    def test_artifact_validator_rejects_extra_fields_and_backdating(self):
        good = self.fetch().records[0]
        for mutation in (
            {"notes": "private"}, {"available_at": good["last_updated"]},
            {"source_url": "https://evil.test/"}, {"source": "fred-alfred"},
            {"availability_basis": "provider_last_updated"}, {"series_id": "dgs10"},
            {"observed_at": "2026-09-07T13:00:01+00:00"},
            {"last_updated": "2026-09-08T00:00:00.000000Z"},
            {"realtime_start": "2026-09-06"},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_metadata_record({**good, **mutation})
        copy = validate_metadata_record(good)
        self.assertIsNot(copy, good)
        self.assertEqual(copy, good)


if __name__ == "__main__":
    unittest.main()
