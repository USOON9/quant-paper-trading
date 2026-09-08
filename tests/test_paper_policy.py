from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, Inexact, ROUND_DOWN, localcontext
import hashlib
import json
import unittest

from quantpaper.paper_policy import evaluate_preflight, policy_contract


class PaperPreflightTests(unittest.TestCase):
    NOW = datetime(2026, 9, 8, 15, tzinfo=timezone.utc)

    def timestamp(self, delta=0):
        return (self.NOW + timedelta(seconds=delta)).isoformat()

    def snapshot(self):
        return {
            "account": {"id": "private-account-id", "status": "ACTIVE", "currency": "USD",
                        "trading_blocked": False, "account_blocked": False, "trade_suspended_by_user": False,
                        "cash": "100", "equity": "100", "last_equity": "100", "buying_power": "100"},
            "clock": {"is_open": True, "timestamp": self.timestamp(), "next_close": self.timestamp(3600)},
            "asset": {"symbol": "SPY", "status": "active", "asset_class": "us_equity", "tradable": True, "fractionable": True},
            "positions": [], "open_orders": [],
            "quote": {"symbol": "SPY", "timestamp": self.timestamp(-1), "bid": "500", "ask": "500.01",
                      "bid_size": "10", "ask_size": "10", "feed": "iex"},
            "capture_started_at": self.timestamp(-2),
        }

    def evaluate(self, snapshot=None, **kwargs):
        return evaluate_preflight(self.snapshot() if snapshot is None else snapshot,
                                  symbol=kwargs.get("symbol", "SPY"), notional=kwargs.get("notional", Decimal("5")),
                                  now=kwargs.get("now", self.NOW))

    def assert_blocked(self, snapshot, code, **kwargs):
        result = self.evaluate(snapshot, **kwargs)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn(code, result["reasons"])
        self.assertFalse(result["execution_authorized"])
        return result

    def test_valid_preflight_is_not_execution_authorization(self):
        result = self.evaluate()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["symbol"], "SPY")
        self.assertEqual(result["notional_usd"], "5.00")
        self.assertEqual(result["quote_age_seconds"], 1)
        self.assertAlmostEqual(result["spread_bps"], 0.19999800002)
        self.assertTrue(result["research_engineering_only"])
        self.assertFalse(result["execution_authorized"])
        self.assertEqual(result["timestamps"]["evaluated_at"], "2026-09-08T15:00:00.000000+00:00")

    def test_output_privacy_and_exact_input_hash(self):
        snapshot = self.snapshot()
        before = deepcopy(snapshot)
        result = self.evaluate(snapshot)
        encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
        self.assertEqual(result["snapshot_hash"], hashlib.sha256(encoded).hexdigest())
        self.assertEqual(snapshot, before)
        for forbidden in ("private-account-id", '"account"', '"cash"', '"buying_power"', '"bid"', '"ask"'):
            self.assertNotIn(forbidden, json.dumps(result))
        snapshot["account"]["id"] = "different-private-id"
        self.assertNotEqual(result["snapshot_hash"], self.evaluate(snapshot)["snapshot_hash"])

    def test_policy_hash_and_mutable_return_isolation(self):
        policy = policy_contract()
        expected = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest()
        self.assertEqual(self.evaluate()["policy_hash"], expected)
        policy["allowed_symbols"].append("JPM")
        self.assertEqual(policy_contract()["allowed_symbols"], ["SPY"])
        self.assertTrue(any("not a commission or fee estimate" in text for text in policy["limitations"]))
        self.assertTrue(any("not an investment recommendation" in text for text in policy["limitations"]))

    def test_symbol_scope_does_not_echo_invalid_symbol(self):
        for symbol in ("JPM", "spy", None, "SPY?secret=private", ["SPY"]):
            result = self.assert_blocked(self.snapshot(), "SYMBOL_NOT_ALLOWED", symbol=symbol)
            self.assertIsNone(result["symbol"])
            self.assertNotIn("secret", json.dumps(result))

    def test_notional_bounds_and_cents(self):
        for amount in (Decimal("1"), Decimal("25"), Decimal("5.00"), Decimal("5.000")):
            self.assertEqual(self.evaluate(notional=amount)["status"], "PASS")
        for amount in (Decimal("0.99"), Decimal("25.01"), Decimal("5.001"), Decimal("-1"),
                       Decimal("NaN"), Decimal("Infinity"), Decimal("sNaN"), True, 5.0, "5"):
            with self.subTest(amount=str(amount)):
                result = self.assert_blocked(self.snapshot(), "NOTIONAL_INVALID", notional=amount)
                self.assertIsNone(result["notional_usd"])

    def test_cash_reserve_and_buying_power_exact_boundaries(self):
        snapshot = self.snapshot()
        snapshot["account"].update(cash="6", buying_power="5")
        self.assertEqual(self.evaluate(snapshot)["status"], "PASS")
        snapshot["account"]["cash"] = "5.99"
        self.assert_blocked(snapshot, "CASH_RESERVE_INSUFFICIENT")
        snapshot["account"]["buying_power"] = "4.99"
        self.assert_blocked(snapshot, "BUYING_POWER_INSUFFICIENT")

    def test_daily_loss_blocks_at_exactly_five_dollars(self):
        snapshot = self.snapshot()
        snapshot["account"].update(last_equity="100", equity="95.01")
        self.assertEqual(self.evaluate(snapshot)["status"], "PASS")
        for equity in ("95", "94.99"):
            snapshot["account"]["equity"] = equity
            self.assert_blocked(snapshot, "ACCOUNT_DAILY_LOSS_LIMIT")

    def test_positive_equities_required(self):
        for field, code in (("equity", "EQUITY_NOT_POSITIVE"), ("last_equity", "LAST_EQUITY_NOT_POSITIVE")):
            for value in ("0", "-1"):
                snapshot = self.snapshot()
                snapshot["account"][field] = value
                self.assert_blocked(snapshot, code)

    def test_account_flags_and_status_strict(self):
        for flag in ("trading_blocked", "account_blocked", "trade_suspended_by_user"):
            for value in (True, None, 0, "false"):
                snapshot = self.snapshot()
                snapshot["account"][flag] = value
                self.assert_blocked(snapshot, "ACCOUNT_BLOCKED")
        for field, value, code in (("status", "PENDING", "ACCOUNT_NOT_ACTIVE"),
                                    ("currency", "GBP", "ACCOUNT_CURRENCY_NOT_USD"),
                                    ("id", "", "ACCOUNT_ID_INVALID")):
            snapshot = self.snapshot()
            snapshot["account"][field] = value
            self.assert_blocked(snapshot, code)

    def test_numeric_strings_strict_in_all_account_fields(self):
        for field in ("cash", "equity", "last_equity", "buying_power"):
            for value in (True, 100, 100.0, "NaN", "Infinity", "1e2", " 100", None, "1" * 65):
                snapshot = self.snapshot()
                snapshot["account"][field] = value
                with self.subTest(field=field, value=value):
                    self.assert_blocked(snapshot, "ACCOUNT_NUMERIC_INVALID")

    def test_whole_account_positions_block_even_other_symbol_or_zero(self):
        for qty in ("1", "0", "-1"):
            snapshot = self.snapshot()
            snapshot["positions"] = [{"symbol": "JPM", "qty": qty, "market_value": "100", "extra": "retained only in hash"}]
            self.assert_blocked(snapshot, "ACCOUNT_NOT_FLAT")
        for positions in (None, {}, [None], [{"symbol": "SPY", "qty": True, "market_value": "0"}], [{}] * 1001):
            snapshot = self.snapshot()
            snapshot["positions"] = positions
            self.assert_blocked(snapshot, "POSITIONS_INVALID")

    def test_all_open_orders_block(self):
        snapshot = self.snapshot()
        snapshot["open_orders"] = [{"symbol": "JPM", "id": "private-order-id"}]
        result = self.assert_blocked(snapshot, "OPEN_ORDERS_PRESENT")
        self.assertNotIn("private-order-id", json.dumps(result))
        for orders in (None, {}, ["order"], [{}] * 1001):
            snapshot["open_orders"] = orders
            self.assert_blocked(snapshot, "OPEN_ORDERS_INVALID")

    def test_asset_must_match_and_be_active_fractionable_equity(self):
        for field, value, code in (
            ("symbol", "JPM", "ASSET_SYMBOL_MISMATCH"), ("status", "inactive", "ASSET_NOT_ACTIVE"),
            ("asset_class", "crypto", "ASSET_NOT_US_EQUITY"), ("tradable", False, "ASSET_NOT_TRADABLE"),
            ("tradable", 1, "ASSET_NOT_TRADABLE"), ("fractionable", False, "ASSET_NOT_FRACTIONABLE"),
        ):
            snapshot = self.snapshot()
            snapshot["asset"][field] = value
            self.assert_blocked(snapshot, code)

    def test_market_open_and_clock_skew_boundaries(self):
        for value in (False, None, 1, "true"):
            snapshot = self.snapshot()
            snapshot["clock"]["is_open"] = value
            self.assert_blocked(snapshot, "MARKET_NOT_OPEN")
        for delta in (-5, 5):
            snapshot = self.snapshot()
            snapshot["clock"]["timestamp"] = self.timestamp(delta)
            self.assertEqual(self.evaluate(snapshot)["status"], "PASS")
        for delta in (-5.000001, 5.000001):
            snapshot = self.snapshot()
            snapshot["clock"]["timestamp"] = self.timestamp(delta)
            self.assert_blocked(snapshot, "CLOCK_SKEW_EXCEEDED")

    def test_collection_age_boundaries_and_future(self):
        snapshot = self.snapshot()
        snapshot["capture_started_at"] = self.timestamp(-10)
        self.assertEqual(self.evaluate(snapshot)["status"], "PASS")
        snapshot["capture_started_at"] = self.timestamp(-10.000001)
        self.assert_blocked(snapshot, "COLLECTION_TOO_SLOW")
        snapshot["capture_started_at"] = self.timestamp(0.000001)
        self.assert_blocked(snapshot, "COLLECTION_START_FUTURE")

    def test_close_buffer_strictly_greater_than_five_minutes(self):
        snapshot = self.snapshot()
        snapshot["clock"]["next_close"] = self.timestamp(300.000001)
        self.assertEqual(self.evaluate(snapshot)["status"], "PASS")
        for delta in (300, 299, -1):
            snapshot["clock"]["next_close"] = self.timestamp(delta)
            self.assert_blocked(snapshot, "CLOSE_BUFFER_INSUFFICIENT")

    def test_quote_age_exactly_two_seconds_passes_future_fails(self):
        snapshot = self.snapshot()
        for delta in (-2, 0):
            snapshot["quote"]["timestamp"] = self.timestamp(delta)
            self.assertEqual(self.evaluate(snapshot)["status"], "PASS")
        snapshot["quote"]["timestamp"] = self.timestamp(-2.000001)
        self.assert_blocked(snapshot, "QUOTE_STALE")
        snapshot["quote"]["timestamp"] = self.timestamp(0.000001)
        self.assert_blocked(snapshot, "QUOTE_FUTURE")

    def test_quote_identity_feed_and_locked_crossed_markets(self):
        snapshot = self.snapshot()
        snapshot["quote"].update(bid="100", ask="100")
        self.assertEqual(self.evaluate(snapshot)["spread_bps"], 0)
        self.assertEqual(self.evaluate(snapshot)["status"], "PASS")
        snapshot["quote"]["ask"] = "99.99"
        self.assert_blocked(snapshot, "QUOTE_CROSSED")
        for field, value, code in (("symbol", "JPM", "QUOTE_SYMBOL_MISMATCH"), ("feed", "sip", "QUOTE_FEED_MISMATCH")):
            snapshot = self.snapshot()
            snapshot["quote"][field] = value
            self.assert_blocked(snapshot, code)

    def test_twenty_bps_spread_inclusive_exact_decimal_comparison(self):
        snapshot = self.snapshot()
        snapshot["quote"].update(bid="99.9", ask="100.1")
        result = self.evaluate(snapshot)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["spread_bps"], 20)
        snapshot["quote"]["ask"] = "100.10000000000000000001"
        self.assert_blocked(snapshot, "SPREAD_TOO_WIDE")

    def test_quotes_require_finite_positive_decimal_strings(self):
        for field in ("bid", "ask", "bid_size", "ask_size"):
            for value in ("0", "-1", "NaN", "inf", True, 10, "1e2", None):
                snapshot = self.snapshot()
                snapshot["quote"][field] = value
                self.assert_blocked(snapshot, "QUOTE_NUMERIC_INVALID")

    def test_utc_explicit_timestamp_requirement(self):
        for value in (None, "2026-09-08T15:00:00", "2026-09-08T16:00:00+01:00", "2026-09-08T15:00:00.000000001Z", "2026-02-30T00:00:00Z"):
            for section, field, code in (("clock", "timestamp", "CLOCK_TIMESTAMP_INVALID"),
                                         ("clock", "next_close", "NEXT_CLOSE_INVALID"),
                                         ("quote", "timestamp", "QUOTE_TIMESTAMP_INVALID")):
                snapshot = self.snapshot()
                snapshot[section][field] = value
                self.assert_blocked(snapshot, code)
            snapshot = self.snapshot()
            snapshot["capture_started_at"] = value
            self.assert_blocked(snapshot, "COLLECTION_START_INVALID")
        for now in (None, self.NOW.replace(tzinfo=None), self.NOW.astimezone(timezone(timedelta(hours=1))), "2026-09-08T15:00:00Z"):
            self.assert_blocked(self.snapshot(), "NOW_INVALID", now=now)

    def test_schema_missing_unknown_and_malformed_fail_closed(self):
        for section in ("account", "clock", "asset", "quote"):
            for replacement in (None, {}, {**self.snapshot()[section], "unknown": 1}):
                snapshot = self.snapshot()
                snapshot[section] = replacement
                self.assert_blocked(snapshot, f"{section.upper()}_SCHEMA_INVALID")
        for snapshot in ({}, {**self.snapshot(), "unknown": 1}, []):
            self.assert_blocked(snapshot, "SNAPSHOT_SCHEMA_INVALID")

    def test_non_json_and_oversized_input_has_no_invented_hash(self):
        for extra in (float("nan"), float("inf"), object(), (1, 2), "x" * 1_000_001):
            snapshot = self.snapshot()
            snapshot["extra"] = extra
            result = self.assert_blocked(snapshot, "SNAPSHOT_NOT_JSON")
            self.assertIsNone(result["snapshot_hash"])
            json.dumps(result, allow_nan=False)

    def test_independent_failures_accumulate_in_fixed_order(self):
        snapshot = self.snapshot()
        snapshot["account"].update(trading_blocked=True, cash="0", buying_power="0", equity="90")
        snapshot["clock"]["is_open"] = False
        snapshot["quote"].update(timestamp=self.timestamp(-10), feed="sip")
        result = self.evaluate(snapshot)
        self.assertTrue({"ACCOUNT_BLOCKED", "ACCOUNT_DAILY_LOSS_LIMIT", "CASH_RESERVE_INSUFFICIENT",
                         "BUYING_POWER_INSUFFICIENT", "MARKET_NOT_OPEN", "QUOTE_STALE", "QUOTE_FEED_MISMATCH"}
                        <= set(result["reasons"]))
        self.assertEqual(result["reasons"], sorted(set(result["reasons"])))
        self.assertTrue(set(result["reasons"]) <= set(policy_contract()["reasons"]))

    def test_ambient_decimal_precision_does_not_change_result(self):
        expected = self.evaluate()
        with localcontext() as context:
            context.prec = 3
            context.rounding = ROUND_DOWN
            context.traps[Inexact] = True
            context.Emax = 2
            context.Emin = -2
            self.assertEqual(self.evaluate(), expected)


if __name__ == "__main__":
    unittest.main()
