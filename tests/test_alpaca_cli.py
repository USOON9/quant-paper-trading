from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from quantpaper.alpaca_cli import main


class AlpacaCommandTests(unittest.TestCase):
    def test_preflight_constructs_get_only_service_and_returns_blocked_code(self):
        service = Mock()
        service.preflight_stock.return_value = {"status": "BLOCKED", "orders_submitted": 0}
        with (patch("quantpaper.alpaca_paper.PaperCredentials.load", return_value="synthetic") as credentials,
              patch("quantpaper.alpaca_paper.AlpacaPaperService", return_value=service) as factory,
              redirect_stdout(io.StringIO())):
            self.assertEqual(main(["preflight", "--symbol", "SPY", "--notional", "5"]), 3)
        credentials.assert_called_once_with(Path(".env"))
        self.assertIs(factory.call_args.kwargs["read_only"], True)
        service.preflight_stock.assert_called_once_with("SPY", Decimal("5"))
        service.round_trip_stock.assert_not_called()

    def test_round_trip_without_explicit_id_fails_before_credentials(self):
        with (patch("quantpaper.alpaca_paper.PaperCredentials.load") as credentials,
              redirect_stderr(io.StringIO())):
            self.assertEqual(main(["round-trip"]), 2)
        credentials.assert_not_called()

    def test_invalid_pilot_request_fails_before_credentials(self):
        for args in (["--symbol", "JPM"], ["--notional", "26"], ["--run-id", "../escape"]):
            with (self.subTest(args=args), patch("quantpaper.alpaca_paper.PaperCredentials.load") as credentials,
                  redirect_stderr(io.StringIO())):
                self.assertEqual(main(["round-trip", "--run-id", "test-001", *args]), 2)
            credentials.assert_not_called()

    def test_read_commands_use_read_only_constructor(self):
        service = Mock()
        service.status.return_value = {"open_orders": 0}
        with (patch("quantpaper.alpaca_paper.PaperCredentials.load", return_value="synthetic"),
              patch("quantpaper.alpaca_paper.AlpacaPaperService", return_value=service) as factory,
              redirect_stdout(io.StringIO())):
            self.assertEqual(main(["status"]), 0)
        self.assertIs(factory.call_args.kwargs["read_only"], True)
        service.round_trip_stock.assert_not_called()

    def test_execution_dispatch_preserves_explicit_run_id_without_enabling_gates(self):
        service = Mock()
        service.round_trip_stock.return_value = {"engineering_test_only": True}
        with (patch("quantpaper.alpaca_paper.PaperCredentials.load", return_value="synthetic"),
              patch("quantpaper.alpaca_paper.AlpacaPaperService", return_value=service) as factory,
              redirect_stdout(io.StringIO())):
            self.assertEqual(main(["round-trip", "--run-id", "approved-synthetic-001"]), 0)
        self.assertIs(factory.call_args.kwargs["read_only"], False)
        service.round_trip_stock.assert_called_once_with("SPY", Decimal("5"), run_id="approved-synthetic-001")

    def test_provider_exception_is_not_echoed(self):
        private_payload = "synthetic-private-provider-response"
        service = Mock()
        service.preflight_stock.side_effect = RuntimeError(private_payload)
        output = io.StringIO()
        with (patch("quantpaper.alpaca_paper.PaperCredentials.load", return_value="synthetic"),
              patch("quantpaper.alpaca_paper.AlpacaPaperService", return_value=service), redirect_stderr(output)):
            self.assertEqual(main(["preflight"]), 2)
        self.assertNotIn(private_payload, output.getvalue())

    def test_invalid_arguments_are_sanitized(self):
        secret = "synthetic-private-argument"
        for args in (["preflight", "--unknown", secret], ["preflight", "--notional", secret],
                     ["submit-cancel"], ["preflight", "--sym", "SPY"]):
            output = io.StringIO()
            with self.subTest(args=args), redirect_stderr(output), self.assertRaises(SystemExit) as error:
                main(args)
            self.assertEqual(error.exception.code, 2)
            self.assertNotIn(secret, output.getvalue())

    def test_help_does_not_load_credentials(self):
        with (patch("quantpaper.alpaca_paper.PaperCredentials.load") as credentials,
              redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as error):
            main(["--help"])
        self.assertEqual(error.exception.code, 0)
        credentials.assert_not_called()

    def test_rehearsal_dispatch_never_loads_credentials_or_constructs_client(self):
        output = io.StringIO()
        result = {"status": "PASS", "orders_submitted_to_alpaca": 0}
        with (patch("quantpaper.paper_rehearsal.rehearse", return_value=result) as rehearsal,
              patch("quantpaper.alpaca_paper.PaperCredentials.load") as credentials,
              patch("quantpaper.alpaca_paper.AlpacaPaperService") as service, redirect_stdout(output)):
            self.assertEqual(main(["rehearse"]), 0)
        self.assertEqual(json.loads(output.getvalue()), result)
        rehearsal.assert_called_once_with()
        credentials.assert_not_called()
        service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
