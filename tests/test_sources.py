from __future__ import annotations

import unittest
import traceback
from urllib.parse import parse_qs, urlsplit

try:
    from quantpaper.sources.fred import FREDVintageClient, parse_fred_observations
    from quantpaper.sources.sec import SECCompanyFactsClient, parse_company_facts
except ImportError:
    parse_fred_observations = None


@unittest.skipIf(parse_fred_observations is None, "data source dependencies are unavailable")
class SourceParsingTests(unittest.TestCase):
    def test_sec_facts_use_day_after_filing(self) -> None:
        payload = {
            "facts": {
                "us-gaap": {
                    "Assets": {
                        "units": {
                            "USD": [
                                {
                                    "end": "2024-12-31",
                                    "val": 123,
                                    "accn": "0001-24-000001",
                                    "fy": 2024,
                                    "fp": "FY",
                                    "form": "10-K",
                                    "filed": "2025-02-14",
                                }
                            ]
                        }
                    }
                }
            }
        }
        records = parse_company_facts(payload, "TEST", concepts=("Assets",))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].available_at, "2025-02-15T05:00:00Z")
        self.assertEqual(records[0].instrument_id, "US:TEST")

    def test_sec_client_requires_identifiable_user_agent(self) -> None:
        with self.assertRaises(ValueError):
            SECCompanyFactsClient("anonymous")

    def test_fred_preserves_revisions_as_separate_vintages(self) -> None:
        payload = {
            "observations": [
                {"realtime_start": "2024-02-01", "date": "2024-01-01", "value": "3.0"},
                {"realtime_start": "2024-03-01", "date": "2024-01-01", "value": "3.1"},
            ]
        }
        records = parse_fred_observations(payload, "TEST")
        self.assertEqual(len(records), 2)
        self.assertNotEqual(records[0].vintage_date, records[1].vintage_date)
        self.assertEqual(records[0].available_at, "2024-02-02T06:00:00Z")

    def test_fred_client_paginates_and_does_not_audit_key(self) -> None:
        pages = [
            {
                "count": 2,
                "observations": [
                    {"realtime_start": "2024-02-01", "date": "2024-01-01", "value": "3"}
                ],
            },
            {
                "count": 2,
                "observations": [
                    {"realtime_start": "2024-03-01", "date": "2024-01-01", "value": "3.1"}
                ],
            },
        ]

        def fake_get_json(url: str):
            params = parse_qs(urlsplit(url).query)
            self.assertEqual(params["output_type"], ["1"])
            self.assertEqual(params["offset"], [str(2 - len(pages))])
            return pages.pop(0)

        batch = FREDVintageClient("a" * 32, get_json=fake_get_json).fetch("test")
        self.assertEqual(len(batch.records), 2)
        self.assertNotIn("api_key", batch.request)

    def test_sec_retains_quarter_ytd_units_and_amendments(self) -> None:
        base = {"end": "2024-06-30", "val": 10, "accn": "original",
                "form": "10-Q", "filed": "2024-08-01", "fy": 2024, "fp": "Q2"}
        payload = {"facts": {"us-gaap": {"Revenues": {"units": {
            "USD": [{**base, "start": "2024-04-01"},
                    {**base, "start": "2024-01-01", "val": 20},
                    {**base, "start": "2024-04-01", "val": 11, "form": "10-Q/A",
                     "accn": "amendment", "filed": "2024-08-10"}],
            "EUR": [{**base, "start": "2024-04-01", "val": 9}],
        }}}}}
        records = parse_company_facts(payload, "TEST", ("Revenues",))
        self.assertEqual(len(records), 4)
        self.assertEqual(len({row.record_id for row in records}), 4)
        self.assertEqual(records[0].available_at, "2024-08-02T04:00:00Z")
        self.assertIn("10-Q/A", {row.form for row in records})

    def test_sec_conflicting_values_are_not_silently_overwritten(self) -> None:
        base = {"end": "2024-06-30", "val": 10, "accn": "original",
                "form": "10-Q", "filed": "2024-08-01"}
        payload = {"facts": {"us-gaap": {"Assets": {"units": {
            "USD": [base, {**base, "val": 99}],
        }}}}}
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            parse_company_facts(payload, "TEST", ("Assets",))

    def test_fred_preserves_real_time_end_and_daylight_saving(self) -> None:
        rows = parse_fred_observations({"observations": [
            {"date": "2024-06-01", "realtime_start": "2024-07-01",
             "realtime_end": "2024-07-31", "value": "."},
        ]}, "TEST")
        self.assertIsNone(rows[0].value)
        self.assertEqual(rows[0].realtime_end, "2024-07-31")
        self.assertEqual(rows[0].available_at, "2024-07-02T05:00:00Z")

    def test_fred_rejects_truncated_pagination(self) -> None:
        pages = [{"count": 2, "observations": [
            {"date": "2024-01-01", "realtime_start": "2024-02-01", "value": "1"}
        ]}, {"count": 2, "observations": []}]
        client = FREDVintageClient("a" * 32, lambda url: pages.pop(0))
        with self.assertRaisesRegex(RuntimeError, "ended before"):
            client.fetch("TEST")

    def test_fred_transport_exception_does_not_leak_secret(self) -> None:
        secret = "never-show-this-test-secret-12345"

        def broken_request(url):
            raise RuntimeError(f"HTTP request failed: {url}")

        try:
            FREDVintageClient(secret, broken_request).fetch("TEST")
        except RuntimeError:
            rendered = traceback.format_exc()
        else:
            self.fail("request should have failed")
        self.assertNotIn(secret, rendered)
        self.assertIn("offset 0", rendered)

    def test_fred_rejects_wrong_output_format(self) -> None:
        with self.assertRaisesRegex(ValueError, "output_type=1"):
            parse_fred_observations({"output_type": 2, "observations": []}, "TEST")


if __name__ == "__main__":
    unittest.main()
