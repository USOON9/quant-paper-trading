import unittest
from unittest.mock import Mock, patch

from alpaca.trading.client import TradingClient
from requests import Session

from quantpaper.paper_transport import PaperHTTPSession, configure_transport


class PaperTransportTests(unittest.TestCase):
    def test_read_only_rejects_all_mutations_and_nonpaper_hosts_before_network(self):
        session = PaperHTTPSession(host="paper-api.alpaca.markets", read_only=True)
        with patch.object(Session, "request") as request:
            for method, url in (("POST", "https://paper-api.alpaca.markets/v2/orders"),
                                ("DELETE", "https://paper-api.alpaca.markets/v2/orders/x"),
                                ("GET", "https://api.alpaca.markets/v2/account"),
                                ("GET", "http://paper-api.alpaca.markets/v2/account"),
                                ("GET", "https://paper-api.alpaca.markets.evil.test/v2/account"),
                                ("GET", "https://user:secret@paper-api.alpaca.markets/v2/account"),
                                ("GET", "https://paper-api.alpaca.markets:444/v2/account"),
                                ("GET", "https://paper-api.alpaca.markets/not-v2/account")):
                with self.subTest(method=method, url=url), self.assertRaises(RuntimeError):
                    session.request(method, url)
            request.assert_not_called()

    def test_timeout_and_redirects_are_fixed_and_implicit_auth_is_disabled(self):
        session = PaperHTTPSession(host="paper-api.alpaca.markets", read_only=False)
        with patch.object(Session, "request", return_value="synthetic") as request:
            self.assertEqual(session.request("POST", "https://paper-api.alpaca.markets/v2/orders",
                                             timeout=None, allow_redirects=True), "synthetic")
            self.assertEqual(request.call_args.kwargs["timeout"], (3.05, 5.0))
            self.assertIs(request.call_args.kwargs["allow_redirects"], False)
            self.assertIs(session.trust_env, False)

    def test_data_transport_is_always_get_only(self):
        session = PaperHTTPSession(host="data.alpaca.markets", read_only=False)
        with patch.object(Session, "request") as request:
            with self.assertRaises(RuntimeError):
                session.request("POST", "https://data.alpaca.markets/v2/stocks")
            request.assert_not_called()

    def test_configuration_disables_sdk_retries_without_any_request(self):
        client = TradingClient("synthetic-key", "synthetic-secret", paper=True)
        old = client._session
        with patch.object(old, "close", wraps=old.close) as close:
            configure_transport(client, host="paper-api.alpaca.markets", read_only=True)
            close.assert_called_once()
        self.assertIsInstance(client._session, PaperHTTPSession)
        self.assertEqual(client._retry, 0)
        client._session.close()

    def test_sdk_does_not_retry_429_posts(self):
        client = TradingClient("synthetic-key", "synthetic-secret", paper=True)
        configure_transport(client, host="paper-api.alpaca.markets", read_only=False)
        response = Mock(status_code=429, text='{"code":429,"message":"synthetic rate limit"}')
        from requests import HTTPError
        response.raise_for_status.side_effect = HTTPError(response=response)
        with patch.object(client._session, "request", return_value=response) as request:
            from alpaca.common.exceptions import APIError
            with self.assertRaises(APIError):
                client.post("/orders", {"client_order_id": "synthetic"})
            self.assertEqual(request.call_count, 1)
        client._session.close()


if __name__ == "__main__":
    unittest.main()
