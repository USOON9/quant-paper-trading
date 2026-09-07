from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from quantpaper.audit import AuditTrail
from quantpaper.alpaca import AlpacaPaperGateway
from quantpaper.domain import AssetClass, Fill, Instrument, Order, OrderStatus, OrderType, Portfolio, Quote, Side
from quantpaper.execution import ExecutionConfig, PaperExchange
from quantpaper.risk import RiskEngine, RiskLimits


class AuditIntegrityTests(unittest.TestCase):
    def test_rejects_middle_record_tampering_before_append(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            audit = AuditTrail(path)
            for index in range(3):
                audit.record("event", index, {"value": index})
            lines = path.read_text().splitlines()
            record = json.loads(lines[1])
            record["data"]["value"] = 999
            lines[1] = json.dumps(record)
            path.write_text("\n".join(lines) + "\n")
            original = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "line 2"):
                AuditTrail(path)
            with self.assertRaises(ValueError):
                audit.record("later", 4, {})
            self.assertEqual(path.read_bytes(), original)

    def test_truncated_final_record_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            AuditTrail(path).record("event", 1, {})
            path.write_bytes(path.read_bytes()[:-1])
            with self.assertRaises(ValueError):
                AuditTrail(path)

    def test_two_existing_writer_instances_share_latest_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            first, second = AuditTrail(path), AuditTrail(path)
            first.record("a", 1, {})
            second.record("b", 2, {})
            first.record("c", 3, {})
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records[1]["previous_hash"], records[0]["hash"])
            self.assertEqual(records[2]["previous_hash"], records[1]["hash"])
            AuditTrail(path)  # All links and payload digests verify on restart.


class ExecutionCausalityTests(unittest.TestCase):
    def setUp(self):
        self.stock = Instrument("SPY", AssetClass.EQUITY, D("0.01"), D("1"))
        self.other = Instrument("QQQ", AssetClass.EQUITY, D("0.01"), D("1"))
        self.venue = PaperExchange(ExecutionConfig(5, D("0")))

    def quote(self, ts, instrument=None, size="100"):
        return Quote(ts, instrument or self.stock, D("99"), D("100"), D(size), D(size))

    def order(self, order_id="a", side=Side.BUY, qty="1", limit=None):
        return Order(order_id, self.stock, side, D(qty), OrderType.LIMIT if limit else OrderType.MARKET, 1, D(limit) if limit else None)

    def test_other_symbol_quote_does_not_fill_stale_quote(self):
        self.venue.on_quote(self.quote(1))
        order = self.order()
        self.venue.submit(order)
        self.assertEqual(self.venue.on_quote(self.quote(10, self.other)), [])
        self.assertEqual(order.status, OrderStatus.PENDING)
        self.assertEqual(len(self.venue.on_quote(self.quote(11))), 1)

    def test_end_of_replay_cancels_without_manufacturing_fills(self):
        self.venue.on_quote(self.quote(1))
        order = self.order()
        self.venue.submit(order)
        self.assertEqual(self.venue.flush(10000), [])
        self.assertEqual(order.status, OrderStatus.CANCELLED)

    def test_slippage_never_breaches_limit_in_either_direction(self):
        for side, limit in [(Side.BUY, "100.00"), (Side.SELL, "99.00")]:
            venue = PaperExchange(ExecutionConfig(0, D("100")))
            venue.submit(self.order(side=side, limit=limit))
            fill = venue.on_quote(self.quote(2))[0]
            self.assertEqual(fill.price, D(limit))

    def test_multiple_orders_cannot_reuse_displayed_liquidity(self):
        self.venue.submit(self.order("a", qty="6"))
        self.venue.submit(self.order("b", qty="6"))
        fills = self.venue.on_quote(self.quote(10, size="10"))
        self.assertEqual(sum((f.quantity for f in fills), D(0)), D(6))
        self.assertEqual(self.venue.orders["b"].status, OrderStatus.PENDING)

    def test_duplicate_order_is_rejected(self):
        self.venue.submit(self.order())
        with self.assertRaises(ValueError):
            self.venue.submit(self.order())

    def test_legacy_gateway_cannot_bypass_audited_order_lifecycle(self):
        gateway = AlpacaPaperGateway("test-key", "test-secret")
        with self.assertRaisesRegex(RuntimeError, "retired"):
            gateway.submit(self.order())
        with self.assertRaisesRegex(RuntimeError, "account-wide"):
            gateway.cancel_all()
        with self.assertRaisesRegex(RuntimeError, "read-only"):
            gateway._request("POST", "/v2/orders", {})

    def test_missing_mark_does_not_erase_open_position_equity(self):
        portfolio = Portfolio(D("1000"))
        portfolio.apply_fill(Fill("a", 1, self.stock, Side.BUY, D("1"), D("100"), D("0")))
        with self.assertRaisesRegex(ValueError, "missing mark"):
            portfolio.equity({}, {"SPY": self.stock})

    def test_pending_orders_reserve_risk_before_they_fill(self):
        risk = RiskEngine(RiskLimits(D("1000"), D("1000"), D("1000"), D("100"), 100), D("10000"))
        pending = self.order("a", qty="6")
        pending.status = OrderStatus.PENDING
        approved, reason = risk.approve(
            self.order("b", qty="6"), self.quote(1), Portfolio(D("10000")),
            {"SPY": D("100")}, {"SPY": self.stock}, pending_orders=[pending],
        )
        self.assertFalse(approved)
        self.assertEqual(reason, "max symbol notional exceeded")


if __name__ == "__main__":
    unittest.main()
