"""Broker-free integration checks for the explicitly authorized paper pilot."""

from __future__ import annotations

from copy import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from quantpaper.alpaca_paper import AlpacaPaperService, PaperCredentials, PaperReconciliationRequired


ACCOUNT_ID = "22222222-2222-4222-8222-222222222222"
OTHER_ACCOUNT_ID = "33333333-3333-4333-8333-333333333333"
ENTRY_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
EXIT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class PaperPilotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.now = datetime.now(timezone.utc)
        self.account = SimpleNamespace(
            id=ACCOUNT_ID, status="ACTIVE", currency="USD", trading_blocked=False,
            account_blocked=False, trade_suspended_by_user=False, cash="100000",
            buying_power="100000", equity="100000", last_equity="100000",
        )
        self.clock = SimpleNamespace(timestamp=self.now, is_open=True,
                                     next_open=self.now + timedelta(days=1),
                                     next_close=self.now + timedelta(hours=1))
        self.asset = SimpleNamespace(symbol="SPY", status="active", asset_class="us_equity",
                                     tradable=True, fractionable=True)
        self.quote = SimpleNamespace(timestamp=self.now, bid_price=500, ask_price=500.02,
                                     bid_size=100, ask_size=100)
        self.trading = Mock()
        self.data = Mock()
        self.trading.get_account.return_value = self.account
        self.trading.get_clock.return_value = self.clock
        self.trading.get_asset.return_value = self.asset
        self.trading.get_all_positions.return_value = []
        self.trading.get_orders.return_value = []
        self.data.get_stock_latest_quote.return_value = {"SPY": self.quote}
        self.orders = {}
        self.trading.submit_order.side_effect = self.accept
        self.trading.get_order_by_id.side_effect = self.fill
        patches = (
            patch("quantpaper.alpaca_paper.TradingClient", return_value=self.trading),
            patch("quantpaper.alpaca_paper.StockHistoricalDataClient", return_value=self.data),
            patch("quantpaper.alpaca_paper.PAPER_STATE_DIRECTORY", self.root / "paper-state"),
            patch("quantpaper.alpaca_paper.PAPER_PILOT_DIRECTORY", self.root / "paper-pilot"),
            patch("quantpaper.alpaca_paper.tempfile.gettempdir", return_value=str(self.root)),
            patch.dict(os.environ, {
                "ENABLE_ALPACA_PAPER": "YES_I_UNDERSTAND",
                "ENABLE_ALPACA_PAPER_ROUND_TRIP": "YES_RUN_SMALL_ROUND_TRIP",
            }),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.credentials = PaperCredentials("synthetic-pilot-key", "synthetic-pilot-secret")
        self.service = AlpacaPaperService(self.credentials, self.root / "audit.jsonl")

    def accept(self, *, order_data):
        side = getattr(order_data.side, "value", order_data.side)
        order_id = ENTRY_ID if side == "buy" else EXIT_ID
        result = SimpleNamespace(
            id=order_id, client_order_id=order_data.client_order_id, symbol=order_data.symbol,
            side=order_data.side, type="market", status="new", filled_qty="0", filled_avg_price=None,
        )
        self.orders[order_id] = result
        return result

    def fill(self, order_id):
        result = copy(self.orders[str(order_id)])
        result.status = "filled"
        result.filled_qty = "0.01"
        result.filled_avg_price = "500"
        return result

    def run_pilot(self, *, service=None, run_id="synthetic-pilot-001", symbol="SPY", notional="5"):
        return (service or self.service).round_trip_stock(symbol, Decimal(notional), run_id=run_id)

    def assert_blocked_before_submit(self):
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_pilot()
        self.trading.submit_order.assert_not_called()
        self.trading.cancel_order_by_id.assert_not_called()

    def test_preflight_is_read_only_and_does_not_authorize_or_consume_attempt(self):
        first = self.service.preflight_stock("SPY", Decimal("5"))
        second = self.service.preflight_stock("SPY", Decimal("5"))
        self.assertEqual(first["status"], "PASS")
        self.assertEqual(second["status"], "PASS")
        self.assertIs(first["execution_authorized"], False)
        self.assertEqual(first["orders_submitted"], 0)
        self.trading.submit_order.assert_not_called()
        self.trading.cancel_order_by_id.assert_not_called()
        self.assertEqual(self.run_pilot()["filled_qty"], "0.01")

    def test_read_only_constructor_does_not_create_an_audit_or_submit(self):
        path = self.root / "read-only" / "audit.jsonl"
        with (patch("quantpaper.alpaca_paper.PAPER_STATE_DIRECTORY", path.parent / "state"),
              patch("quantpaper.alpaca_paper.PAPER_PILOT_DIRECTORY", path.parent / "pilot"),
              patch("quantpaper.alpaca_paper.tempfile.gettempdir", return_value=str(path.parent))):
            service = AlpacaPaperService(self.credentials, path, read_only=True)
            self.assertIsNone(service.audit)
            self.assertFalse(path.parent.exists())
            self.assertEqual(service.preflight_stock("SPY", Decimal("5"))["status"], "PASS")
            with self.assertRaisesRegex(RuntimeError, "read-only"):
                self.run_pilot(service=service)
        self.assertFalse(path.parent.exists())
        self.trading.submit_order.assert_not_called()

    def test_aware_non_utc_broker_timestamps_normalize_and_pass(self):
        eastern_offset = timezone(timedelta(hours=-4))
        self.clock.timestamp = self.clock.timestamp.astimezone(eastern_offset)
        self.clock.next_close = self.clock.next_close.astimezone(eastern_offset)
        self.quote.timestamp = self.quote.timestamp.astimezone(eastern_offset)
        snapshot = self.service._capture_preflight("SPY")
        for section, field in (("clock", "timestamp"), ("clock", "next_close"), ("quote", "timestamp")):
            normalized = datetime.fromisoformat(snapshot[section][field])
            self.assertEqual(normalized.utcoffset(), timedelta(0))
        self.assertEqual(self.service.preflight_stock("SPY", Decimal("5"))["status"], "PASS")
        self.assertEqual(self.run_pilot()["filled_qty"], "0.01")

    def test_naive_broker_quote_timestamp_fails_closed(self):
        self.quote.timestamp = self.now.replace(tzinfo=None)
        self.assert_blocked_before_submit()

    def test_missing_gates_block_before_broker_reads_or_orders(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assert_blocked_before_submit()
        self.trading.get_account.assert_not_called()

    def test_each_gate_is_independently_required(self):
        for name in ("ENABLE_ALPACA_PAPER", "ENABLE_ALPACA_PAPER_ROUND_TRIP"):
            with self.subTest(name=name), patch.dict(os.environ, {name: "NO"}):
                self.assert_blocked_before_submit()
        self.trading.get_account.assert_not_called()

    def test_legacy_submit_cancel_cannot_bypass_account_guard(self):
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            self.service.submit_cancel_smoke("SPY", Decimal("1"))
        self.trading.get_account.assert_not_called()
        self.trading.submit_order.assert_not_called()

    def test_only_spy_whole_cent_bounded_notional_and_explicit_id(self):
        for options in ({"symbol": "JPM"}, {"notional": "0.99"}, {"notional": "25.01"},
                        {"notional": "1.001"}, {"notional": "NaN"}, {"run_id": ""},
                        {"run_id": "../escape"}, {"run_id": None}):
            with self.subTest(options=options), self.assertRaises((ValueError, RuntimeError)):
                self.run_pilot(**options)
        with self.assertRaises(TypeError):
            self.service.round_trip_stock("SPY", Decimal("5"))
        self.trading.get_account.assert_not_called()
        self.trading.submit_order.assert_not_called()

    def test_other_symbol_position_blocks_whole_account_pilot(self):
        self.trading.get_all_positions.return_value = [
            SimpleNamespace(symbol="JPM", qty="1", market_value="200")]
        self.assert_blocked_before_submit()

    def test_other_symbol_open_order_blocks_whole_account_pilot(self):
        self.trading.get_orders.return_value = [SimpleNamespace(symbol="JPM", id="synthetic-existing")]
        self.assert_blocked_before_submit()

    def test_stale_quote_blocks_preflight_without_order_mutations(self):
        self.quote.timestamp = self.now - timedelta(seconds=3)
        result = self.service.preflight_stock("SPY", Decimal("5"))
        self.assertEqual(result["status"], "BLOCKED")
        self.assert_blocked_before_submit()

    def test_future_quote_and_stale_broker_clock_block(self):
        self.quote.timestamp = self.now + timedelta(minutes=1)
        self.assert_blocked_before_submit()
        self.quote.timestamp = self.now
        self.clock.timestamp = self.now - timedelta(seconds=6)
        self.assert_blocked_before_submit()

    def test_close_buffer_and_closed_market_block(self):
        self.clock.next_close = self.now + timedelta(minutes=4)
        self.assert_blocked_before_submit()
        self.clock.next_close = self.now + timedelta(hours=1)
        self.clock.is_open = False
        self.assert_blocked_before_submit()

    def test_invalid_spread_size_and_nonfractionable_asset_block(self):
        for field, value in (("ask_price", 502), ("ask_price", 499),
                             ("bid_size", 0), ("ask_price", float("nan"))):
            previous = getattr(self.quote, field)
            setattr(self.quote, field, value)
            with self.subTest(field=field, value=value):
                self.assert_blocked_before_submit()
            setattr(self.quote, field, previous)
        self.asset.fractionable = False
        self.assert_blocked_before_submit()

    def test_cash_reserve_and_daily_loss_proxy_block_entry(self):
        self.account.cash = "5.99"
        self.assert_blocked_before_submit()
        self.account.cash = "100000"
        self.account.equity = "99995"
        self.assert_blocked_before_submit()

    def test_success_uses_two_bound_order_ids_and_proves_whole_account_flat(self):
        result = self.run_pilot()
        self.assertEqual(result["entry_order_id"], ENTRY_ID)
        self.assertEqual(result["exit_order_id"], EXIT_ID)
        self.assertEqual(result["run_id"], "synthetic-pilot-001")
        self.assertIs(result["engineering_test_only"], True)
        self.assertEqual(result["post_trade_status"], {
            "positions": [], "open_orders": 0, "account_identity_verified": True})
        requests = [call.kwargs["order_data"] for call in self.trading.submit_order.call_args_list]
        self.assertEqual(len(requests), 2)
        self.assertNotEqual(requests[0].client_order_id, requests[1].client_order_id)
        self.assertEqual(requests[0].to_request_fields()["notional"], "5")
        self.assertEqual(requests[1].to_request_fields()["qty"], "0.01")
        self.assertFalse(self.service._incident_path.exists())

    def test_second_run_id_same_day_cannot_submit_another_attempt(self):
        self.run_pilot()
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_pilot(run_id="synthetic-pilot-002")
        self.assertEqual(self.trading.submit_order.call_count, 2)

    def test_reusing_completed_run_id_is_blocked(self):
        self.run_pilot()
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_pilot()
        self.assertEqual(self.trading.submit_order.call_count, 2)

    def test_same_account_different_key_and_audit_cannot_bypass_daily_budget(self):
        self.run_pilot()
        credentials = PaperCredentials("synthetic-other-pilot-key", "synthetic-other-pilot-secret")
        other = AlpacaPaperService(credentials, self.root / "other" / "audit.jsonl")
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_pilot(service=other, run_id="synthetic-other-001")
        self.assertEqual(self.trading.submit_order.call_count, 2)

    def test_unknown_submit_stops_once_and_remains_blocked_under_other_key(self):
        self.trading.submit_order.side_effect = TimeoutError("synthetic transport failure")
        self.trading.get_order_by_client_id.side_effect = TimeoutError("synthetic lookup failure")
        with self.assertRaises(PaperReconciliationRequired):
            self.run_pilot()
        other = AlpacaPaperService(PaperCredentials("synthetic-rotated-key", "synthetic-secret"),
                                   self.root / "rotated" / "audit.jsonl")
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_pilot(service=other, run_id="synthetic-retry-001")
        self.assertEqual(self.trading.submit_order.call_count, 1)

    def test_account_identity_change_after_exit_prevents_success(self):
        def submit(*, order_data):
            result = self.accept(order_data=order_data)
            if str(getattr(order_data.side, "value", order_data.side)) == "sell":
                changed = copy(self.account)
                changed.id = OTHER_ACCOUNT_ID
                self.trading.get_account.return_value = changed
            return result
        self.trading.submit_order.side_effect = submit
        with self.assertRaisesRegex(PaperReconciliationRequired, "identity changed"):
            self.run_pilot()
        self.assertEqual(self.trading.submit_order.call_count, 2)
        self.assertTrue(self.service._incident_path.exists())

    def test_other_symbol_position_after_exit_prevents_flat_success(self):
        def submit(*, order_data):
            result = self.accept(order_data=order_data)
            if str(getattr(order_data.side, "value", order_data.side)) == "sell":
                self.trading.get_all_positions.return_value = [
                    SimpleNamespace(symbol="JPM", qty="1", market_value="200")]
            return result
        self.trading.submit_order.side_effect = submit
        with self.assertRaisesRegex(PaperReconciliationRequired, "not fully flat"):
            self.run_pilot()
        self.assertTrue(self.service._incident_path.exists())

    def test_stale_quote_and_changed_permissions_do_not_block_risk_reducing_exit(self):
        def fill(order_id):
            result = self.fill(order_id)
            if str(order_id) == ENTRY_ID:
                self.quote.timestamp = self.now - timedelta(hours=1)
                self.asset.fractionable = False
                self.clock.is_open = False
            return result
        self.trading.get_order_by_id.side_effect = fill
        result = self.run_pilot()
        self.assertIs(result["post_trade_status"]["account_identity_verified"], True)
        self.assertEqual(self.trading.submit_order.call_count, 2)

    def test_nonfinite_fill_prices_cannot_complete_a_pilot(self):
        def fill(order_id):
            result = self.fill(order_id)
            if str(order_id) == EXIT_ID:
                result.filled_avg_price = "NaN"
            return result
        self.trading.get_order_by_id.side_effect = fill
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_pilot()
        self.assertTrue(self.service._incident_path.exists())

    def test_expired_quote_after_order_intent_audit_blocks_before_network_submit(self):
        clock = [self.now]

        class ClockType(type):
            def __instancecheck__(cls, value):
                return isinstance(value, datetime)

        class ControlledDateTime(datetime, metaclass=ClockType):
            @classmethod
            def now(cls, tz=None):
                return clock[0]

        audit_type = type(self.service.audit)
        original_record = audit_type.record
        def record(audit, event, timestamp, payload):
            result = original_record(audit, event, timestamp, payload)
            if event == "alpaca_paper_order_intent":
                clock[0] += timedelta(seconds=3)
            return result
        with (patch("quantpaper.alpaca_paper.datetime", ControlledDateTime),
              patch.object(audit_type, "record", new=record)):
            self.assert_blocked_before_submit()

    def test_wrong_symbol_response_cannot_produce_an_exit_for_unrelated_quantity(self):
        def submit(*, order_data):
            result = self.accept(order_data=order_data)
            result.symbol = "JPM"
            return result
        self.trading.submit_order.side_effect = submit
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_pilot()
        self.assertEqual(self.trading.submit_order.call_count, 1)
        self.assertTrue(self.service._incident_path.exists())

    def test_wrong_polled_order_identity_cannot_complete_the_entry(self):
        def fill(order_id):
            result = self.fill(order_id)
            result.id = EXIT_ID
            return result
        self.trading.get_order_by_id.side_effect = fill
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_pilot()
        self.assertEqual(self.trading.submit_order.call_count, 1)
        self.assertTrue(self.service._incident_path.exists())

    def test_regressing_cumulative_fill_quantity_is_not_accepted(self):
        quantities = iter(("0.009", "0.008"))
        def partial(order_id):
            result = self.fill(order_id)
            result.status = "partially_filled"
            result.filled_qty = next(quantities)
            return result
        self.trading.get_order_by_id.side_effect = partial
        with patch("quantpaper.alpaca_paper.time.sleep"), self.assertRaises((RuntimeError, ValueError)):
            self.run_pilot()
        self.assertEqual(self.trading.submit_order.call_count, 1)
        self.assertTrue(self.service._incident_path.exists())

    def test_fill_quantity_cannot_decrease_after_submission_acknowledgment(self):
        def submit(*, order_data):
            result = self.accept(order_data=order_data)
            if str(result.id) == ENTRY_ID:
                result.status = "partially_filled"
                result.filled_qty = "0.02"
                result.filled_avg_price = "250"
            return result

        self.trading.submit_order.side_effect = submit
        with self.assertRaises(PaperReconciliationRequired):
            self.run_pilot()
        self.assertEqual(self.trading.submit_order.call_count, 1)
        self.assertTrue(self.service._incident_path.exists())

    def test_fill_quantity_cannot_decrease_across_cancel_reconciliation(self):
        observations = iter(("partial", "timeout", "terminal"))

        def poll(order_id):
            if str(order_id) == EXIT_ID:
                return self.fill(order_id)
            observation = next(observations)
            if observation == "timeout":
                raise TimeoutError("synthetic polling timeout")
            result = self.fill(order_id)
            if observation == "partial":
                result.status = "partially_filled"
                result.filled_qty = "0.02"
                result.filled_avg_price = "250"
            return result

        self.trading.get_order_by_id.side_effect = poll
        with patch("quantpaper.alpaca_paper.time.sleep"), self.assertRaises(PaperReconciliationRequired):
            self.run_pilot()
        self.assertEqual(self.trading.submit_order.call_count, 1)
        self.trading.cancel_order_by_id.assert_called_once_with(ENTRY_ID)
        self.assertTrue(self.service._incident_path.exists())


if __name__ == "__main__":
    unittest.main()
