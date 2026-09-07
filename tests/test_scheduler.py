from __future__ import annotations

from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import os

try:
    from quantpaper.scheduler import eligible_symbols, require_paper_gates_closed
except ImportError:
    eligible_symbols = None


@unittest.skipIf(eligible_symbols is None, "calendar dependencies are unavailable")
class SchedulerTests(unittest.TestCase):
    def test_equities_wait_until_exchange_close_delay(self) -> None:
        symbols = ["SPY", "BTC-USD"]
        before_close = datetime(2026, 9, 4, 19, 0, tzinfo=timezone.utc)
        after_close = datetime(2026, 9, 4, 20, 31, tzinfo=timezone.utc)
        self.assertEqual(eligible_symbols(symbols, before_close), [])
        self.assertEqual(eligible_symbols(symbols, after_close), ["SPY"])

    def test_catchup_on_weekends_keeps_next_equity_target(self) -> None:
        symbols = ["SPY", "BTC-USD"]
        saturday = datetime(2026, 9, 5, 22, 0, tzinfo=timezone.utc)
        labor_day = datetime(2026, 9, 7, 22, 0, tzinfo=timezone.utc)
        self.assertEqual(eligible_symbols(symbols, saturday), ["SPY"])
        self.assertEqual(eligible_symbols(symbols, labor_day), ["SPY"])

    def test_scheduler_requires_closed_order_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            gates = {"ENABLE_ALPACA_PAPER": "YES_I_UNDERSTAND",
                     "ENABLE_ALPACA_PAPER_ROUND_TRIP": "YES_RUN_SMALL_ROUND_TRIP"}
            for open_gate in gates:
                with self.subTest(open_gate=open_gate):
                    path.write_text("\n".join(
                        f"{name}={enabled if name == open_gate else 'NO'}" for name, enabled in gates.items()
                    ) + "\n")
                    # This exercises file configuration, not the caller's
                    # environment override (CI intentionally sets both to NO).
                    # Restore every inherited variable when this scope exits.
                    with patch.dict(os.environ):
                        for name in gates:
                            os.environ.pop(name, None)
                        with self.assertRaises(RuntimeError):
                            require_paper_gates_closed(path)

    def test_environment_open_gate_overrides_closed_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/".env"
            path.write_text("ENABLE_ALPACA_PAPER=NO\n")
            with patch.dict(os.environ,{"ENABLE_ALPACA_PAPER":"YES_I_UNDERSTAND"}):
                with self.assertRaises(RuntimeError):
                    require_paper_gates_closed(path)

    def test_early_close_and_dst_are_calendar_driven(self) -> None:
        self.assertEqual(eligible_symbols(["SPY"],datetime(2026,11,27,18,29,tzinfo=timezone.utc)),[])
        self.assertEqual(eligible_symbols(["SPY"],datetime(2026,11,27,18,31,tzinfo=timezone.utc)),["SPY"])


if __name__ == "__main__":
    unittest.main()
