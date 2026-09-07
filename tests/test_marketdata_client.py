from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import traceback
import unittest
from unittest.mock import patch

from quantpaper.marketdata.client import MarketDataClient, MarketDataError


START = datetime(2024, 1, 3, 15, tzinfo=timezone.utc)
END = START + timedelta(minutes=1)
BAR = {"t": "2024-01-03T15:00:00Z", "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 500}
QUOTE = {"t": "2024-01-03T15:00:01.123456789Z", "bp": 100, "ap": 100.02, "bs": 10, "as": 12}
TEST_KEY = "fixture_key_not_real"
TEST_SECRET = "fixture_secret_not_real"


class Response:
    def __init__(self, payload=None, status=200, *, url=None, history=None):
        self.payload = payload
        self.status_code = status
        self.url = url
        self.history = history or []
        self.json_calls = 0

    def json(self):
        self.json_calls += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True

    def post(self, *args, **kwargs):
        raise AssertionError("POST must never be used")

    def delete(self, *args, **kwargs):
        raise AssertionError("DELETE must never be used")


def page(kind="bars", symbol="SPY", records=None, token=None):
    records = records if records is not None else [BAR if kind == "bars" else QUOTE]
    return Response({kind: {symbol: records}, "next_page_token": token})


class MarketDataClientTests(unittest.TestCase):
    def client(self, *responses):
        session = Session(*responses)
        return MarketDataClient(TEST_KEY, TEST_SECRET, session=session), session

    def test_stock_get_is_fixed_host_raw_minute_data_and_no_redirects(self):
        client, session = self.client(page())
        result = client.fetch("bars", "spy", START, END)
        self.assertEqual(len(session.calls), 1)
        url, request = session.calls[0]
        self.assertEqual(url, "https://data.alpaca.markets/v2/stocks/bars")
        self.assertFalse(request["allow_redirects"])
        self.assertEqual(request["timeout"], (5.0, 20.0))
        self.assertEqual(request["headers"]["APCA-API-SECRET-KEY"], TEST_SECRET)
        self.assertEqual(request["params"], {
            "symbols": "SPY", "start": "2024-01-03T15:00:00Z", "end": "2024-01-03T15:01:00Z",
            "sort": "asc", "limit": 10000, "feed": "sip", "timeframe": "1Min", "adjustment": "raw",
        })
        self.assertTrue(result["complete"])
        self.assertIsNone(result["truncation_reason"])
        self.assertEqual(result["records"], [BAR])
        self.assertNotIn(TEST_KEY, json.dumps(result))
        self.assertNotIn(TEST_SECRET, repr(client))
        self.assertEqual(result["content_hash"], hashlib.sha256(json.dumps(
            [BAR], sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode()).hexdigest())

    def test_public_crypto_normalizes_alias_and_never_uses_equity_feed_query(self):
        session = Session(page("quotes", "BTC/USD"))
        client = MarketDataClient(session=session)
        result = client.fetch("quotes", "BTC-USD", START, END)
        url, request = session.calls[0]
        self.assertEqual(url, "https://data.alpaca.markets/v1beta3/crypto/us/quotes")
        self.assertEqual(result["symbol"], "BTC/USD")
        self.assertEqual(result["feed"], "crypto_us")
        self.assertNotIn("feed", request["params"])
        self.assertNotIn("timeframe", request["params"])
        self.assertNotIn("APCA-API-KEY-ID", request["headers"])
        self.assertEqual(result["records"][0]["t"], QUOTE["t"])

    def test_all_four_routes_are_available_but_no_base_override(self):
        for symbol, base in [("SPY", "/v2/stocks"), ("BTC/USD", "/v1beta3/crypto/us")]:
            for kind in ("bars", "quotes"):
                client, session = self.client(page(kind, symbol))
                client.fetch(kind, symbol, START, END)
                self.assertEqual(session.calls[0][0], "https://data.alpaca.markets" + base + "/" + kind)
        with self.assertRaises(TypeError):
            MarketDataClient(base_url="https://untrusted.example")

    def test_redirect_is_rejected_without_auth_forward_or_json_read(self):
        redirect = Response({"secret": TEST_SECRET}, status=302, url="https://untrusted.example")
        client, session = self.client(redirect)
        with self.assertRaises(MarketDataError) as raised:
            client.fetch("bars", "SPY", START, END)
        self.assertEqual(raised.exception.category, "redirect")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(redirect.json_calls, 0)
        self.assertNotIn(TEST_SECRET, str(raised.exception))

    def test_auth_permission_rate_limit_errors_are_sanitized_and_not_retried(self):
        for status, category in [(401, "authentication"), (403, "permission"), (429, "rate_limit"), (500, "http")]:
            response = Response({"details": TEST_SECRET}, status=status)
            client, session = self.client(response)
            with self.subTest(status=status), self.assertRaises(MarketDataError) as raised:
                client.fetch("quotes", "SPY", START, END)
            self.assertEqual(raised.exception.category, category)
            self.assertEqual(raised.exception.http_status, status)
            self.assertEqual(len(session.calls), 1)
            self.assertEqual(response.json_calls, 0)
            self.assertNotIn(TEST_SECRET, repr(raised.exception))

    def test_transport_and_json_exceptions_do_not_reveal_details_or_chains(self):
        for response in [RuntimeError("private URL " + TEST_SECRET),
                         Response(ValueError("private body " + TEST_SECRET))]:
            client, _ = self.client(response)
            try:
                client.fetch("bars", "SPY", START, END)
            except MarketDataError:
                rendered = traceback.format_exc()
            else:
                self.fail("Expected sanitized failure")
            self.assertNotIn(TEST_SECRET, rendered)
            self.assertNotIn("private URL", rendered)
            self.assertNotIn("private body", rendered)

    def test_pagination_completion_cap_and_tokens(self):
        client, session = self.client(page(token="opaque-1"), page(records=[{**BAR, "v": 700}]))
        result = client.fetch("bars", "SPY", START, END)
        self.assertEqual(result["pages"], 2)
        self.assertEqual(session.calls[1][1]["params"]["page_token"], "opaque-1")
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["records"]), 2)
        self.assertNotIn("opaque-1", json.dumps(result))
        client, session = self.client(page(token="more"))
        result = client.fetch("bars", "SPY", START, END, max_pages=1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["truncation_reason"], "max_pages_reached")
        self.assertEqual(len(session.calls), 1)

    def test_repeated_or_malformed_token_is_never_called_complete(self):
        client, session = self.client(page(token="same"), page(token="same"))
        with self.assertRaises(MarketDataError) as raised:
            client.fetch("bars", "SPY", START, END)
        self.assertEqual(raised.exception.category, "pagination")
        self.assertEqual(len(session.calls), 2)
        for token in (1, "", "\n", [], {}):
            client, _ = self.client(page(token=token))
            with self.subTest(token=token), self.assertRaises(MarketDataError):
                client.fetch("bars", "SPY", START, END)

    def test_malformed_symbol_mapping_records_and_missing_token_fail_closed(self):
        payloads = [
            [], {}, {"bars": {}, "next_page_token": None, "ignored": "metadata"},
            {"bars": {"SPY": [BAR]}}, {"bars": [], "next_page_token": None},
            {"bars": {"JPM": [BAR]}, "next_page_token": None},
            {"bars": {"SPY": [BAR]}, "symbol": "JPM", "next_page_token": None},
            {"bars": {"SPY": [{**BAR, "symbol": "JPM"}]}, "next_page_token": None},
            {"bars": {"SPY": [{**BAR, "S": "JPM"}]}, "next_page_token": None},
            {"bars": {"SPY": [{**BAR, "t": "2024-01-03"}]}, "next_page_token": None},
            {"bars": {"SPY": [{**BAR, "c": float("nan")}]}, "next_page_token": None},
            {"bars": {"SPY": [None]}, "next_page_token": None},
        ]
        for index, payload in enumerate(payloads):
            client, _ = self.client(Response(payload))
            if index == 2:  # An explicit empty mapping is valid completed retrieval.
                self.assertEqual(client.fetch("bars", "SPY", START, END)["records"], [])
                continue
            with self.subTest(index=index), self.assertRaises(MarketDataError) as raised:
                client.fetch("bars", "SPY", START, END)
            self.assertEqual(raised.exception.category, "malformed_response")

    def test_invalid_routes_symbols_feeds_and_windows_make_no_requests(self):
        client, session = self.client()
        with self.assertRaises(MarketDataError):
            client._get_page("/v2/orders", {})
        cases = [
            ("orders", "SPY", START, END, {}), ([], "SPY", START, END, {}),
            ("bars", "SPY/JPM", START, END, {}), ("bars", "AAPL", START, END, {}),
            ("bars", "SPY", START, END, {"feed": "https://untrusted.example"}),
            ("bars", "BTC/USD", START, END, {"feed": "iex"}),
            ("bars", "SPY", START, END, {"max_pages": True}),
            ("bars", "SPY", START, END, {"max_pages": 11}),
            ("bars", "SPY", START.replace(tzinfo=None), END, {}),
            ("bars", "SPY", START, START, {}),
            ("bars", "SPY", START, START + timedelta(days=2), {}),
            ("bars", "SPY", datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(hours=1), {}),
        ]
        for kind, symbol, start, end, kwargs in cases:
            with self.subTest(kind=kind, symbol=symbol, kwargs=kwargs), self.assertRaises(MarketDataError):
                client.fetch(kind, symbol, start, end, **kwargs)
        self.assertEqual(session.calls, [])
        with self.assertRaises(MarketDataError):
            MarketDataClient(session=session).fetch("bars", "SPY", START, END)

    def test_from_env_has_no_environment_mutation_and_never_reveals_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(f"APCA_API_KEY_ID={TEST_KEY}\nAPCA_API_SECRET_KEY={TEST_SECRET}\nUNRELATED=ignored\n")
            with patch.dict(os.environ, {"UNRELATED_EXISTING": "kept"}, clear=True):
                before = dict(os.environ)
                with patch("quantpaper.marketdata.client.requests.Session", return_value=Session()) as factory:
                    # Supply a mockable production-shaped session without ever
                    # making HTTP calls or using real credentials.
                    session = factory.return_value
                    session.mount = lambda *args, **kwargs: None
                    client = MarketDataClient.from_env(path)
                self.assertEqual(before, dict(os.environ))
                self.assertNotIn(TEST_KEY, repr(client))
                self.assertNotIn(TEST_SECRET, str(client))
                self.assertFalse(session.trust_env)
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(MarketDataError) as raised:
                    MarketDataClient.from_env(Path(directory) / "missing.env")
                self.assertEqual(raised.exception.category, "configuration")

    def test_environment_precedence_and_invalid_credentials_are_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("APCA_API_KEY_ID=file_key\nAPCA_API_SECRET_KEY=file_secret\n")
            session = Session(page())
            session.mount = lambda *args, **kwargs: None
            with patch.dict(os.environ, {"APCA_API_KEY_ID": TEST_KEY, "APCA_API_SECRET_KEY": TEST_SECRET}, clear=True):
                with patch("quantpaper.marketdata.client.requests.Session", return_value=session):
                    client = MarketDataClient.from_env(path)
                client.fetch("bars", "SPY", START, END)
            self.assertEqual(session.calls[0][1]["headers"]["APCA-API-KEY-ID"], TEST_KEY)
        for key, secret in [(TEST_KEY, None), (None, TEST_SECRET), (TEST_KEY, "bad\nsecret")]:
            with self.assertRaises(MarketDataError) as raised:
                MarketDataClient(key, secret, session=Session())
            self.assertNotIn(TEST_SECRET, str(raised.exception))

    def test_observed_at_is_after_fetch_and_inclusive_boundary_is_preserved(self):
        boundary = {**BAR, "t": "2024-01-03T15:01:00Z"}
        client, _ = self.client(page(records=[boundary]))
        before = datetime.now(timezone.utc)
        result = client.fetch("bars", "SPY", START, END)
        after = datetime.now(timezone.utc)
        observed = datetime.fromisoformat(result["observed_at"].replace("Z", "+00:00"))
        self.assertLessEqual(before, observed)
        self.assertLessEqual(observed, after)
        self.assertEqual(result["records"], [boundary])
        self.assertEqual(len(result["page_observed_at"]), 1)

    def test_every_page_and_subsequent_fetch_are_paced_by_monotonic_clock(self):
        # monotonic() has no promised epoch/sign; only differences are meaningful.
        clock = {"now": -10.0, "sleeps": [], "starts": []}

        def sleep(seconds):
            clock["sleeps"].append(seconds)
            clock["now"] += seconds

        class TimedSession(Session):
            def get(self, url, **kwargs):
                clock["starts"].append(clock["now"])
                # Network time counts toward the gap; don't blindly add a full
                # delay after every response.
                clock["now"] += 0.1
                return super().get(url, **kwargs)

        session = TimedSession(page(token="next"), page(), page())
        client = MarketDataClient(TEST_KEY, TEST_SECRET, session=session,
                                  min_request_interval_seconds=0.4)
        with patch("quantpaper.marketdata.client.time.monotonic", side_effect=lambda: clock["now"]), \
             patch("quantpaper.marketdata.client.time.sleep", side_effect=sleep):
            client.fetch("bars", "SPY", START, END)
            client.fetch("bars", "SPY", START, END)
        self.assertEqual(client.request_count, 3)
        self.assertEqual(len(clock["sleeps"]), 2)
        for difference in (clock["starts"][1] - clock["starts"][0],
                           clock["starts"][2] - clock["starts"][1]):
            self.assertAlmostEqual(difference, 0.4)
        for duration in clock["sleeps"]:
            self.assertAlmostEqual(duration, 0.3)

    def test_attempt_count_includes_failed_get_but_not_validation_rejection(self):
        client, session = self.client(RuntimeError("synthetic transport error"))
        with self.assertRaises(MarketDataError):
            client._get_page("/v2/orders", {})
        self.assertEqual(client.request_count, 0)
        with self.assertRaises(MarketDataError):
            client.fetch("bars", "SPY", START, END)
        self.assertEqual(client.request_count, 1)
        self.assertEqual(len(session.calls), 1)

    def test_default_pacing_never_sleeps(self):
        client, _ = self.client(page(token="next"), page())
        with patch("quantpaper.marketdata.client.time.sleep") as sleep:
            client.fetch("bars", "SPY", START, END)
        sleep.assert_not_called()
        self.assertEqual(client.request_count, 2)

    def test_invalid_pacing_is_rejected_before_session_creation(self):
        for interval in (-0.1, 5.1, float("nan"), float("inf"), True, "0.4", None):
            with self.subTest(interval=interval), patch("quantpaper.marketdata.client.requests.Session") as factory:
                with self.assertRaises(MarketDataError):
                    MarketDataClient(TEST_KEY, TEST_SECRET, min_request_interval_seconds=interval)
                factory.assert_not_called()

    def test_from_env_forwards_pacing_without_changing_process_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(f"APCA_API_KEY_ID={TEST_KEY}\nAPCA_API_SECRET_KEY={TEST_SECRET}\n")
            session = Session(page(token="next"), page())
            session.mount = lambda *args, **kwargs: None
            fake_now = [100.0]

            def sleep(seconds):
                fake_now[0] += seconds

            with patch.dict(os.environ, {}, clear=True):
                before = dict(os.environ)
                with patch("quantpaper.marketdata.client.requests.Session", return_value=session):
                    client = MarketDataClient.from_env(path, min_request_interval_seconds=0.4)
                with patch("quantpaper.marketdata.client.time.monotonic", side_effect=lambda: fake_now[0]), \
                     patch("quantpaper.marketdata.client.time.sleep", side_effect=sleep) as sleeper:
                    client.fetch("bars", "SPY", START, END)
                self.assertEqual(dict(os.environ), before)
            self.assertEqual(client.request_count, 2)
            sleeper.assert_called_once()
            self.assertAlmostEqual(sleeper.call_args.args[0], 0.4)


if __name__ == "__main__":
    unittest.main()
