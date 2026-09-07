from __future__ import annotations

import unittest
import json
import tempfile
from decimal import Decimal
from pathlib import Path

from quantpaper.audit import AuditTrail
from quantpaper.cli import build_engine
from quantpaper.data import synthetic_quotes
from quantpaper.domain import AssetClass, Fill, Instrument, Portfolio, Side
from quantpaper.options import black_scholes


ROOT = Path(__file__).resolve().parents[1]


class PortfolioTests(unittest.TestCase):
    def test_equity_round_trip_uses_option_multiplier(self) -> None:
        option = Instrument("TESTC", AssetClass.OPTION, Decimal("0.01"), Decimal("1"), Decimal("100"))
        portfolio = Portfolio(Decimal("10000"))
        portfolio.apply_fill(Fill("a", 1, option, Side.BUY, Decimal("1"), Decimal("2"), Decimal("0")))
        portfolio.apply_fill(Fill("b", 2, option, Side.SELL, Decimal("1"), Decimal("3"), Decimal("0")))
        self.assertEqual(portfolio.cash, Decimal("10100"))
        self.assertEqual(portfolio.position("TESTC").realized_pnl, Decimal("100"))


class OptionTests(unittest.TestCase):
    def test_put_call_parity(self) -> None:
        call = black_scholes(100, 100, 0.5, 0.2, rate=0.03, is_call=True)
        put = black_scholes(100, 100, 0.5, 0.2, rate=0.03, is_call=False)
        expected = 100 - 100 * __import__("math").exp(-0.03 * 0.5)
        self.assertAlmostEqual(call.price - put.price, expected, places=10)
        self.assertGreater(call.delta, 0)
        self.assertLess(put.delta, 0)


class IntegrationTests(unittest.TestCase):
    def test_three_asset_demo_is_deterministic(self) -> None:
        engine, instruments, seed = build_engine(ROOT / "configs/paper.toml")
        first = engine.run(synthetic_quotes(instruments, 100, seed)).to_dict()
        engine, instruments, seed = build_engine(ROOT / "configs/paper.toml")
        second = engine.run(synthetic_quotes(instruments, 100, seed)).to_dict()
        self.assertEqual(first, second)
        self.assertGreater(first["fills"], 0)
        asset_classes = {instrument.asset_class for instrument in instruments}
        self.assertEqual(asset_classes, {AssetClass.EQUITY, AssetClass.CRYPTO, AssetClass.OPTION})

    def test_audit_records_form_a_hash_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            audit = AuditTrail(path)
            audit.record("first", 1, {"value": Decimal("1.25")})
            audit.record("second", 2, {})
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records[1]["previous_hash"], records[0]["hash"])

    def test_audit_chain_continues_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            AuditTrail(path).record("first", 1, {"value": 1})
            AuditTrail(path).record("second", 2, {"value": 2})
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records[1]["previous_hash"], records[0]["hash"])


if __name__ == "__main__":
    unittest.main()
