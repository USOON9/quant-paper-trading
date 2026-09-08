from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    from quantpaper.alpaca_paper import AlpacaPaperService, ExactMarketOrderRequest, PaperCredentials, PaperReconciliationRequired
    from quantpaper.audit import AuditTrail
    from alpaca.trading.enums import OrderSide, TimeInForce
except ImportError:
    AlpacaPaperService = None


def order(order_id, status, qty="0"):
    order_id = {"entry": "00000000-0000-4000-8000-000000000001",
                "exit": "00000000-0000-4000-8000-000000000002"}.get(order_id, order_id)
    return SimpleNamespace(id=order_id, client_order_id="client-" + order_id,
                           status=status, filled_qty=qty, filled_avg_price="100")


@unittest.skipIf(AlpacaPaperService is None, "paper optional dependencies missing")
class PaperLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name).resolve()
        self.service = AlpacaPaperService.__new__(AlpacaPaperService)
        self.service.trading = Mock()
        self.service.audit = AuditTrail(root / "audit.jsonl")
        self.service._lock_path = root / "operation.lock"
        self.service._incident_path = root / "reconciliation.json"
        self.service._legacy_incident_paths = ()
        self.run_id = "synthetic-lifecycle-001"
        pilot_path = patch("quantpaper.alpaca_paper.PAPER_PILOT_DIRECTORY", root / "paper-pilot", create=True)
        pilot_path.start()
        self.addCleanup(pilot_path.stop)
        lock_path = patch("quantpaper.alpaca_paper.tempfile.gettempdir", return_value=str(root))
        lock_path.start()
        self.addCleanup(lock_path.stop)
        self.service.trading.get_account.return_value = SimpleNamespace(
            id="11111111-1111-4111-8111-111111111111", status="ACTIVE", currency="USD",
            trading_blocked=False, account_blocked=False, trade_suspended_by_user=False,
            cash="100000", buying_power="100000",
            equity="100000", last_equity="100000",
        )
        now = datetime.now(timezone.utc)
        self.service.trading.get_clock.return_value = SimpleNamespace(
            is_open=True, timestamp=now, next_open=now + timedelta(days=1),
            next_close=now + timedelta(hours=1),
        )
        self.service.trading.get_asset.return_value = SimpleNamespace(
            symbol="SPY", status="active", asset_class="us_equity", tradable=True, fractionable=True,
        )
        self.service.stock_data = Mock()
        self.service.stock_data.get_stock_latest_quote.return_value = {
            "SPY": SimpleNamespace(timestamp=now, bid_price=500, ask_price=500.02,
                                   bid_size=100, ask_size=100),
        }
        self.service.trading.get_all_positions.return_value = []
        self.service.trading.get_orders.return_value = []
        self.service.status = Mock(return_value={"positions": [], "open_orders": 0})
        gates = patch.dict(os.environ, {
            "ENABLE_ALPACA_PAPER": "YES_I_UNDERSTAND",
            "ENABLE_ALPACA_PAPER_ROUND_TRIP": "YES_RUN_SMALL_ROUND_TRIP",
        })
        gates.start()
        self.addCleanup(gates.stop)

    def prepare_orders(self, submitted, *, states=(), settled=None):
        submitted = iter(submitted)
        states = iter(states)
        requests = {}

        def bind(result, request):
            result.client_order_id = request.client_order_id
            result.symbol = request.symbol
            result.side = request.side
            result.type = "market"
            return result

        def submit(*, order_data):
            result = bind(next(submitted), order_data)
            requests[str(result.id)] = order_data
            return result

        def poll(order_id):
            return bind(next(states), requests[str(order_id)])

        self.service.trading.submit_order.side_effect = submit
        self.service.trading.get_order_by_id.side_effect = poll
        if settled is not None:
            settled = iter(settled)
            def settle(order_id, *args, **kwargs):
                return bind(next(settled), requests[str(order_id)])
            self.service._settle_order = Mock(side_effect=settle)

    @patch("quantpaper.alpaca_paper.time.sleep")
    def test_partially_filled_is_not_terminal_or_filled(self, sleep):
        self.service.trading.get_order_by_id.side_effect = [
            order("entry", "partially_filled", "0.002"), order("entry", "filled", "0.005"),
        ]
        final = self.service._wait_for_fill("entry", 1)
        self.assertEqual(final.filled_qty, "0.005")
        self.assertEqual(self.service.trading.get_order_by_id.call_count, 2)

    @patch("quantpaper.alpaca_paper.time.sleep")
    def test_partial_entry_cancellation_unwinds_only_confirmed_quantity(self, sleep):
        exact_qty = "0.006487141"
        self.prepare_orders([order("entry", "new"), order("exit", "new")], states=[
            order("entry", "partially_filled", "0.001"), order("entry", "canceled", exact_qty),
            order("exit", "filled", exact_qty),
        ])
        result = self.service.round_trip_stock("SPY", Decimal("5"), run_id=self.run_id)
        request = self.service.trading.submit_order.call_args_list[1].kwargs["order_data"]
        self.assertEqual(request.to_request_fields()["qty"], exact_qty)
        self.assertEqual(result["filled_qty"], exact_qty)
        self.assertFalse(self.service._incident_path.exists())

    def test_sdk_payload_preserves_decimal_without_float_roundtrip(self):
        exact = Decimal("0.123456789123456789")
        request = ExactMarketOrderRequest(symbol="SPY", qty=exact, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
        self.assertEqual(request.to_request_fields()["qty"], str(exact))

    def test_existing_symbol_position_blocks_before_order_submission(self):
        self.service.trading.get_all_positions.return_value = [SimpleNamespace(symbol="SPY", qty="1")]
        with self.assertRaisesRegex(PaperReconciliationRequired, "existing position"):
            self.service.round_trip_stock("SPY", Decimal("5"), run_id=self.run_id)
        self.service.trading.submit_order.assert_not_called()

    def test_ambiguous_submission_recovers_same_id_without_resubmission(self):
        self.service.trading.submit_order.side_effect = TimeoutError("network")
        self.service.trading.get_order_by_client_id.return_value = order("accepted", "new")
        request = ExactMarketOrderRequest(symbol="SPY", notional=Decimal("5"), side=OrderSide.BUY,
                                          time_in_force=TimeInForce.DAY, client_order_id="chosen-id")
        self.assertEqual(self.service._submit(request).id, "accepted")
        self.service.trading.submit_order.assert_called_once()
        self.service.trading.get_order_by_client_id.assert_called_once_with("chosen-id")

    def test_unknown_submission_leaves_marker_and_blocks_restart(self):
        self.service.trading.submit_order.side_effect = TimeoutError("network")
        self.service.trading.get_order_by_client_id.side_effect = TimeoutError("network")
        with self.assertRaisesRegex(PaperReconciliationRequired, "outcome unknown"):
            self.service.round_trip_stock("SPY", Decimal("5"), run_id=self.run_id)
        self.assertTrue(self.service._incident_path.exists())
        with self.assertRaisesRegex(PaperReconciliationRequired, "previous lifecycle"):
            self.service.round_trip_stock("SPY", Decimal("5"), run_id=self.run_id)
        self.service.trading.submit_order.assert_called_once()

    def test_alternate_audit_path_cannot_bypass_key_scoped_incident(self):
        root = Path(self.directory.name).resolve()
        with (
            patch("quantpaper.alpaca_paper.TradingClient", return_value=self.service.trading),
            patch("quantpaper.alpaca_paper.StockHistoricalDataClient"),
            patch("quantpaper.alpaca_paper.PAPER_STATE_DIRECTORY", root / "data" / "paper-state"),
        ):
            credentials = PaperCredentials("synthetic-test-key", "synthetic-test-secret")
            first = AlpacaPaperService(credentials, root / "first" / "audit.jsonl")
            first.stock_data = self.service.stock_data
            self.service.trading.submit_order.side_effect = TimeoutError("network")
            self.service.trading.get_order_by_client_id.side_effect = TimeoutError("network")
            with self.assertRaisesRegex(PaperReconciliationRequired, "outcome unknown"):
                first.round_trip_stock("SPY", Decimal("5"), run_id=self.run_id)
            second = AlpacaPaperService(credentials, root / "different" / "audit.jsonl")
            self.assertEqual(first._incident_path, second._incident_path)
            self.assertTrue(second._incident_path.exists())
            self.service.trading.get_account.reset_mock()
            with self.assertRaisesRegex(PaperReconciliationRequired, "previous lifecycle"):
                second.round_trip_stock("SPY", Decimal("5"), run_id=self.run_id)
            self.service.trading.get_account.assert_not_called()
            self.service.trading.submit_order.assert_called_once()

    def test_legacy_adjacent_marker_is_preserved_and_promoted_to_key_block(self):
        root = Path(self.directory.name).resolve()
        audit_path = root / "legacy-audit.jsonl"
        legacy_path = audit_path.with_suffix(".reconciliation.json")
        legacy_path.write_text('{"symbol":"SPY"}')
        with (
            patch("quantpaper.alpaca_paper.TradingClient", return_value=self.service.trading),
            patch("quantpaper.alpaca_paper.StockHistoricalDataClient"),
            patch("quantpaper.alpaca_paper.PAPER_STATE_DIRECTORY", root / "data" / "paper-state"),
        ):
            credentials = PaperCredentials("legacy-synthetic-test-key", "synthetic-test-secret")
            first = AlpacaPaperService(credentials, audit_path)
            with self.assertRaisesRegex(PaperReconciliationRequired, "legacy lifecycle"):
                first.round_trip_stock("SPY", Decimal("5"), run_id=self.run_id)
            self.assertTrue(legacy_path.exists())
            self.assertTrue(first._incident_path.exists())
            second = AlpacaPaperService(credentials, root / "another-audit.jsonl")
            with self.assertRaisesRegex(PaperReconciliationRequired, "previous lifecycle"):
                second.round_trip_stock("SPY", Decimal("5"), run_id=self.run_id)
            self.service.trading.submit_order.assert_not_called()

    def test_cancel_ack_is_not_claimed_as_confirmed_cancellation(self):
        self.service._wait_for_terminal = Mock(side_effect=TimeoutError("still pending_cancel"))
        with self.assertRaisesRegex(PaperReconciliationRequired, "only requested"):
            self.service._cancel_and_reconcile("entry")

    def test_partial_exit_reports_residual_and_retains_reconciliation_marker(self):
        self.prepare_orders([order("entry", "new"), order("exit", "new")], settled=[
            order("entry", "filled", "0.05"), order("exit", "canceled", "0.02")])
        with self.assertRaisesRegex(PaperReconciliationRequired, "residual qty 0.03"):
            self.service.round_trip_stock("SPY", Decimal("5"), run_id=self.run_id)
        self.assertTrue(self.service._incident_path.exists())
        self.assertEqual(self.service.trading.submit_order.call_count, 2)

    def test_nonfinite_notional_is_rejected_without_broker_access(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.assertRaises(ValueError):
                self.service.round_trip_stock("SPY", Decimal(value), run_id=self.run_id)
        self.service.trading.get_account.assert_not_called()


if __name__ == "__main__":
    unittest.main()
