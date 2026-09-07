from __future__ import annotations

import unittest
import traceback
from unittest.mock import Mock, patch
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

    def test_sec_rejects_boolean_fact_values_instead_of_numeric_coercion(self) -> None:
        for value in (False, True):
            observation = {"end": "2024-06-30", "val": value, "accn": "synthetic",
                           "form": "10-Q", "filed": "2024-08-01"}
            payload = {"facts": {"us-gaap": {"Assets": {"units": {"USD": [observation]}}}}}
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Invalid SEC fact"):
                parse_company_facts(payload, "TEST", ("Assets",))

    def test_fred_rejects_boolean_values_even_before_the_observation_window(self) -> None:
        for value in (False, True):
            for observation_date in ("2024-09-01", "2024-09-07"):
                observation = {"date": observation_date, "realtime_start": "2024-09-08", "value": value}
                with self.subTest(value=value, observation_date=observation_date):
                    with self.assertRaisesRegex(ValueError, "Invalid FRED"):
                        parse_fred_observations({"observations": [observation]}, "TEST")
                    getter = Mock(return_value={"count": 1, "observations": [observation]})
                    with self.assertRaisesRegex(ValueError, "Invalid FRED"):
                        FREDVintageClient("a" * 32, getter).fetch(
                            "TEST", observation_start="2024-09-07",
                            realtime_start="2024-09-07", realtime_end="2024-12-31")
                    getter.assert_called_once()

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

    def test_sec_requires_unique_numeric_mapping_and_matching_payload_identity(self):
        mapping = {"0": {"ticker": "TEST", "cik_str": 1234}}
        getter = Mock(side_effect=[mapping, {"cik": "0000001234", "facts": {}}])
        batch = SECCompanyFactsClient("Synthetic Research research@example.invalid", getter).fetch(" test ")
        self.assertEqual(batch.request["cik"], "0000001234")
        self.assertEqual(batch.request["ticker"], "TEST")
        self.assertEqual(batch.records, [])
        self.assertIn("CIK0000001234.json", getter.call_args.args[0])
        for value in (None, True, "../escape", "12345678901", "0", "12.5"):
            with self.subTest(cik=value):
                getter = Mock(return_value={"0": {"ticker": "TEST", "cik_str": value}})
                with self.assertRaises(ValueError):
                    SECCompanyFactsClient("Synthetic research@example.invalid", getter).fetch("TEST")
                self.assertEqual(getter.call_count, 1)
        for payload in ({"cik": 9999}, {"facts": {}}, {"cik": True}):
            with self.subTest(payload=payload):
                getter = Mock(side_effect=[mapping, payload])
                with self.assertRaises(ValueError):
                    SECCompanyFactsClient("Synthetic research@example.invalid", getter).fetch("TEST")
        getter = Mock(return_value={"0": mapping["0"], "1": mapping["0"]})
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            SECCompanyFactsClient("Synthetic research@example.invalid", getter).fetch("TEST")

    def test_sec_invalid_ticker_or_header_never_fetches_and_errors_hide_provider_text(self):
        getter = Mock()
        client = SECCompanyFactsClient("Synthetic research@example.invalid", getter)
        for ticker in ("../TEST", "TEST?x=1", "TEST\nHEADER", ""):
            with self.subTest(ticker=ticker), self.assertRaises(ValueError):
                client.fetch(ticker)
        getter.assert_not_called()
        with self.assertRaises(ValueError):
            SECCompanyFactsClient("Synthetic research@example.invalid\nInjected: value", getter)
        sentinel = "synthetic-private-user-agent-value"
        getter.side_effect = RuntimeError(sentinel)
        with self.assertRaisesRegex(RuntimeError, "SEC request failed") as caught:
            client.fetch("TEST")
        self.assertNotIn(sentinel, "".join(traceback.format_exception(caught.exception)))

    def test_fred_explicit_window_is_sent_and_request_audit_has_no_key(self):
        captured = []
        def get(url):
            captured.append(parse_qs(urlsplit(url).query))
            return {"count": 1, "observations": [{"date": "2024-01-01", "realtime_start": "2024-02-01", "value": "1"}]}
        batch = FREDVintageClient("synthetic-private-fred-key", get).fetch(
            "test", observation_start="2024-01-01", realtime_start="2024-01-15", realtime_end="2024-03-01")
        self.assertEqual(captured[0]["observation_start"], ["2024-01-01"])
        self.assertEqual(captured[0]["observation_end"], ["2024-03-01"])
        self.assertEqual(captured[0]["realtime_start"], ["2024-01-15"])
        self.assertEqual(captured[0]["realtime_end"], ["2024-03-01"])
        self.assertEqual(batch.request["realtime_start"], "2024-01-15")
        self.assertNotIn("api_key", batch.request)
        self.assertNotIn("synthetic-private-fred-key", repr(batch))

    def test_fred_invalid_date_bounds_fail_before_requests(self):
        getter = Mock()
        client = FREDVintageClient("a" * 32, getter)
        for kwargs in ({"observation_start": "20240101"}, {"realtime_end": "2024-13-01"},
                       {"realtime_end": "1770-01-01"},
                       {"realtime_start": "20240101"}, {"realtime_start": "2024-13-01"},
                       {"realtime_start": "1770-01-01"},
                       {"realtime_start": "2024-03-01", "realtime_end": "2024-02-01"},
                       {"observation_start": "2024-03-01", "realtime_end": "2024-02-01"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                client.fetch("TEST", **kwargs)
        getter.assert_not_called()

    def test_fred_legacy_call_preserves_original_real_time_defaults(self):
        getter = Mock(return_value={"count": 0, "observations": []})
        batch = FREDVintageClient("a" * 32, getter).fetch("TEST")
        params = parse_qs(urlsplit(getter.call_args.args[0]).query)
        self.assertEqual(params["realtime_start"], ["1776-07-04"])
        self.assertEqual(params["realtime_end"], ["9999-12-31"])
        self.assertNotIn("observation_start", params)
        self.assertNotIn("observation_end", params)
        self.assertEqual(batch.request["realtime_start"], "1776-07-04")

    def test_fred_vintage_lower_bound_is_verified_without_fallback(self):
        observation = {"date": "2024-01-01", "realtime_start": "2024-01-14", "value": "1"}
        getter = Mock(return_value={"count": 1, "observations": [observation]})
        client = FREDVintageClient("a" * 32, getter)
        with self.assertRaisesRegex(ValueError, "vintage precedes"):
            client.fetch("TEST", observation_start="2024-01-01",
                         realtime_start="2024-01-15", realtime_end="2024-03-01")
        getter.assert_called_once()
        observation["realtime_start"] = "2024-01-15"
        batch = client.fetch("TEST", observation_start="2024-01-01",
                             realtime_start="2024-01-15", realtime_end="2024-03-01")
        self.assertEqual(batch.records[0].vintage_date, "2024-01-15")
        self.assertEqual(batch.request["realtime_start"], "2024-01-15")

    def test_fred_rejects_out_of_window_records(self):
        for observation in ({"date": "2024-01-01", "realtime_start": "2024-04-01", "value": "1"},
                            {"date": "2024-04-01", "realtime_start": "2024-01-01", "value": "1"}):
            getter = Mock(return_value={"count": 1, "observations": [observation]})
            with self.subTest(observation=observation), self.assertRaises(ValueError):
                FREDVintageClient("a" * 32, getter).fetch(
                    "TEST", observation_start="2024-01-01", realtime_end="2024-03-01")

    def test_fred_filters_earlier_boundary_observations_with_explicit_counts_and_full_hash(self):
        from quantpaper.sources.common import canonical_hash
        observations = [
            {"date": "2024-09-01", "realtime_start": "2024-09-08", "value": "1"},
            {"date": "2024-09-07", "realtime_start": "2024-09-08", "value": "2"},
            {"date": "2024-10-01", "realtime_start": "2024-10-02", "value": "3"},
        ]
        getter = Mock(return_value={"count": 3, "observations": observations})
        batch = FREDVintageClient("a" * 32, getter).fetch(
            "TEST", observation_start="2024-09-07", realtime_start="2024-09-07", realtime_end="2024-12-31")
        self.assertEqual([row.observation_date for row in batch.records], ["2024-09-07", "2024-10-01"])
        self.assertEqual(batch.request["provider_record_count"], 3)
        self.assertEqual(batch.request["parsed_record_count"], 3)
        self.assertEqual(batch.request["excluded_before_observation_start"], 1)
        self.assertEqual(batch.content_hash, canonical_hash({"observations": observations}))
        self.assertNotEqual(batch.content_hash, canonical_hash({"observations": observations[1:]}))
        getter.assert_called_once()

    def test_fred_excluded_rows_are_still_validated_before_filtering(self):
        for observation in (
            {"date": "2024-09-01", "realtime_start": "2024-09-08", "value": "NaN"},
            {"date": "2024-09-01", "realtime_start": "2024-09-08"},
            {"date": "2024-09-01", "realtime_start": "2025-01-01", "value": "1"},
            {"date": "2024-09-01", "realtime_start": "2024-09-06", "value": "1"},
        ):
            getter = Mock(return_value={"count": 1, "observations": [observation]})
            with self.subTest(observation=observation), self.assertRaises(ValueError):
                FREDVintageClient("a" * 32, getter).fetch(
                    "TEST", observation_start="2024-09-07", realtime_start="2024-09-07", realtime_end="2024-12-31")
            getter.assert_called_once()

    def test_fred_all_filtered_rows_return_an_explicit_empty_batch(self):
        getter = Mock(return_value={"count": 1, "observations": [
            {"date": "2024-09-01", "realtime_start": "2024-09-08", "value": "1"}
        ]})
        batch = FREDVintageClient("a" * 32, getter).fetch(
            "TEST", observation_start="2024-09-07", realtime_start="2024-09-07", realtime_end="2024-12-31")
        self.assertEqual(batch.records, [])
        self.assertEqual(batch.request["provider_record_count"], 1)
        self.assertEqual(batch.request["parsed_record_count"], 1)
        self.assertEqual(batch.request["excluded_before_observation_start"], 1)
        getter.assert_called_once()

    def test_fred_pagination_caps_and_no_truncated_success(self):
        getter = Mock(return_value={"count": 200001, "observations": []})
        with self.assertRaisesRegex(RuntimeError, "observation budget"):
            FREDVintageClient("a" * 32, getter).fetch("TEST")
        getter.assert_called_once()
        pages = [{"count": 11, "offset": i, "observations": [
            {"date": "2024-01-01", "realtime_start": "2024-02-01", "value": str(i)}
        ]} for i in range(10)]
        getter = Mock(side_effect=pages)
        with self.assertRaisesRegex(RuntimeError, "page budget"):
            FREDVintageClient("a" * 32, getter).fetch("TEST")
        self.assertEqual(getter.call_count, 10)

    def test_fred_metadata_types_are_strict_and_provider_values_are_not_echoed(self):
        sentinel = "synthetic-private-provider-value"
        for field in ("count", "offset", "output_type"):
            for value in (sentinel, True, 1.5, -1):
                payload = {"count": 0, "offset": 0, "output_type": 1, "observations": [], field: value}
                getter = Mock(return_value=payload)
                with self.subTest(field=field, value=value), self.assertRaises(ValueError) as caught:
                    FREDVintageClient("a" * 32, getter).fetch("TEST")
                self.assertNotIn(sentinel, "".join(traceback.format_exception(caught.exception)))
        with self.assertRaisesRegex(ValueError, "object"):
            parse_fred_observations({"observations": [None]}, "TEST")


class SourceHTTPTests(unittest.TestCase):
    def setUp(self):
        from quantpaper.sources import http
        self.http = http
        self.clock = 0.0
        self.sleeps = []
        def sleep(delay):
            self.sleeps.append(delay)
            self.clock += delay
        for name, replacement in (("_last_started", None),):
            patcher = patch.object(http, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, replacement in (("monotonic", lambda: self.clock), ("sleep", sleep)):
            patcher = patch.object(http.time, name, side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.url = "https://api.stlouisfed.org/fred/series/observations?api_key=synthetic-private-key"
        self.response = Mock(status=200, headers={})
        self.response.__enter__ = Mock(return_value=self.response)
        self.response.__exit__ = Mock(return_value=False)
        self.response.geturl.return_value = self.url
        self.response.read.side_effect = lambda size: b'{"valid":true}'[:size]
        self.opener = Mock()
        self.opener.open.return_value = self.response
        patcher = patch.object(http, "build_opener", return_value=self.opener)
        self.factory = patcher.start()
        self.addCleanup(patcher.stop)

    def get(self, url=None, max_bytes=100):
        return self.http.get_json(url or self.url, {"User-Agent": "Synthetic research"},
                                  allowed_hosts=frozenset({"api.stlouisfed.org"}), max_bytes=max_bytes)

    def test_get_disables_proxies_redirects_and_paces_each_attempt_without_real_sleep(self):
        for _ in range(3):
            self.assertEqual(self.get(), {"valid": True})
        self.assertEqual(self.sleeps, [0.25, 0.25])
        self.assertEqual(self.opener.open.call_count, 3)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(self.opener.open.call_args.kwargs, {"timeout": 30})
        proxy, redirect, https = self.factory.call_args.args
        self.assertEqual(proxy.proxies, {})
        self.assertEqual(https._context.verify_mode, self.http.ssl.CERT_REQUIRED)
        self.assertTrue(https._context.check_hostname)
        with self.assertRaisesRegex(self.http.SourceHTTPError, "redirect"):
            redirect.redirect_request(request, None, 302, "Moved", {}, "https://untrusted.invalid")

    def test_nonofficial_origin_auth_port_and_fragments_never_open(self):
        for url in ("http://api.stlouisfed.org/fred", "https://untrusted.invalid/fred",
                    "https://api.stlouisfed.org.untrusted.invalid/fred",
                    "https://user:password@api.stlouisfed.org/fred",
                    "https://api.stlouisfed.org:444/fred", "https://api.stlouisfed.org/fred#fragment"):
            with self.subTest(url=url), self.assertRaises(self.http.SourceHTTPError):
                self.get(url)
        self.opener.open.assert_not_called()

    def test_declared_and_actual_response_size_are_both_bounded(self):
        self.response.headers = {"Content-Length": "101"}
        with self.assertRaisesRegex(self.http.SourceHTTPError, "response_too_large"):
            self.get(max_bytes=100)
        self.response.read.assert_not_called()
        self.response.headers = {}
        with self.assertRaisesRegex(self.http.SourceHTTPError, "response_too_large"):
            self.get(max_bytes=5)
        self.response.read.assert_called_once_with(6)

    def test_json_ambiguity_nonfinite_and_transport_errors_stay_sanitized(self):
        for payload in (b'{"duplicate":1,"duplicate":2}', b'{"value":NaN}', b'not-json'):
            self.response.read.side_effect = None
            self.response.read.return_value = payload
            with self.subTest(payload=payload), self.assertRaisesRegex(self.http.SourceHTTPError, "invalid_response"):
                self.get()
        sentinel = "synthetic-private-request-url"
        self.opener.open.side_effect = RuntimeError(sentinel)
        with self.assertRaises(self.http.SourceHTTPError) as caught:
            self.get()
        self.assertNotIn(sentinel, "".join(traceback.format_exception(caught.exception)))
        self.assertEqual(self.opener.open.call_count, 4)

    def test_source_wrappers_apply_exact_official_host_and_body_caps(self):
        from quantpaper.sources import fred, sec
        with patch.object(sec, "bounded_get_json", return_value={}) as getter:
            sec._default_get_json(sec.SEC_TICKERS, {"User-Agent": "Synthetic research@example.invalid"})
        self.assertEqual(getter.call_args.kwargs,
                         {"allowed_hosts": frozenset({"data.sec.gov", "www.sec.gov"}), "max_bytes": 25_000_000})
        with patch.object(fred, "bounded_get_json", return_value={}) as getter:
            fred._default_get_json(self.url)
        self.assertEqual(getter.call_args.kwargs,
                         {"allowed_hosts": frozenset({"api.stlouisfed.org"}), "max_bytes": 10_000_000})

    def test_https_context_loads_certifi_and_retains_required_verification(self):
        context = Mock(verify_mode=self.http.ssl.CERT_REQUIRED, check_hostname=True)
        with (patch.object(self.http.ssl, "create_default_context", return_value=context) as factory,
              patch.object(self.http.certifi, "where", return_value="/synthetic/ca-bundle.pem")):
            self.assertEqual(self.get(), {"valid": True})
        factory.assert_called_once_with()
        context.load_verify_locations.assert_called_once_with(cafile="/synthetic/ca-bundle.pem")
        self.assertIs(self.factory.call_args.args[2]._context, context)
        self.assertEqual(context.verify_mode, self.http.ssl.CERT_REQUIRED)
        self.assertIs(context.check_hostname, True)

    def test_missing_ca_bundle_and_unverified_context_never_open_a_connection(self):
        sentinel = "synthetic-private-ca-configuration-detail"
        for broken_bundle in (True, False):
            context = Mock(verify_mode=self.http.ssl.CERT_NONE, check_hostname=False)
            if broken_bundle:
                context.load_verify_locations.side_effect = OSError(sentinel)
            with self.subTest(broken_bundle=broken_bundle), patch.object(
                self.http.ssl, "create_default_context", return_value=context
            ), self.assertRaises(self.http.SourceHTTPError) as caught:
                self.get()
            self.assertEqual(caught.exception.category, "tls_configuration")
            self.assertNotIn(sentinel, "".join(traceback.format_exception(caught.exception)))
        self.factory.assert_not_called()
        self.opener.open.assert_not_called()

    def test_certificate_failures_are_distinct_sanitized_and_never_retried(self):
        sentinel = "synthetic-private-certificate-request-url"
        certificate = self.http.ssl.SSLCertVerificationError(1, sentinel)
        wrapped = self.http.URLError(certificate)
        chained = RuntimeError(sentinel)
        chained.__cause__ = wrapped
        for count, error in enumerate((certificate, wrapped, chained), 1):
            self.opener.open.side_effect = error
            with self.subTest(kind=type(error).__name__), self.assertRaises(self.http.SourceHTTPError) as caught:
                self.get()
            self.assertEqual(caught.exception.category, "tls_verification")
            self.assertNotIn(sentinel, "".join(traceback.format_exception(caught.exception)))
            self.assertEqual(self.opener.open.call_count, count)
            context = self.factory.call_args.args[2]._context
            self.assertEqual(context.verify_mode, self.http.ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)


if __name__ == "__main__":
    unittest.main()
