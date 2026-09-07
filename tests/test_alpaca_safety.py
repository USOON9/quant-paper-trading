from __future__ import annotations

import os
import unittest
from unittest.mock import patch

try:
    from quantpaper.alpaca_paper import AlpacaPaperService
except ImportError:
    AlpacaPaperService = None


@unittest.skipIf(AlpacaPaperService is None, "paper optional dependencies are not installed")
class AlpacaSafetyTests(unittest.TestCase):
    def test_trade_gate_defaults_closed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "safety gate is disabled"):
                AlpacaPaperService._require_gate("ENABLE_ALPACA_PAPER")

    def test_trade_gate_requires_exact_phrase(self) -> None:
        with patch.dict(os.environ, {"ENABLE_ALPACA_PAPER": "yes"}, clear=True):
            with self.assertRaises(RuntimeError):
                AlpacaPaperService._require_gate("ENABLE_ALPACA_PAPER")

    def test_round_trip_gate_uses_separate_phrase(self) -> None:
        environment = {"ENABLE_ALPACA_PAPER_ROUND_TRIP": "YES_RUN_SMALL_ROUND_TRIP"}
        with patch.dict(os.environ, environment, clear=True):
            AlpacaPaperService._require_gate(
                "ENABLE_ALPACA_PAPER_ROUND_TRIP", "YES_RUN_SMALL_ROUND_TRIP"
            )


if __name__ == "__main__":
    unittest.main()
