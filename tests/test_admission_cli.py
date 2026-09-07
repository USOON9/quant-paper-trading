"""Command-line admission dispatch must stay offline and require evidence."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from quantpaper.marketdata.cli import main


class AdmissionCliTests(unittest.TestCase):
    def test_requires_source_before_dispatch(self):
        with patch("quantpaper.admission_runner.run_admission") as runner, redirect_stderr(io.StringIO()):
            self.assertEqual(main(["admission", "--run-dir", "unused"]), 2)
        runner.assert_not_called()

    def test_rejects_date_override(self):
        with patch("quantpaper.admission_runner.run_admission") as runner, redirect_stderr(io.StringIO()):
            self.assertEqual(main(["admission", "--source-dir", "frozen", "--run-dir", "unused",
                                   "--stock-session", "2026-09-01"]), 2)
        runner.assert_not_called()

    def test_successful_audit_is_not_trading_approval(self):
        report = {"contract": "offline_intent_admission_v1", "status": "audited"}
        visible = {"status": "audited", "execution_enabled": False, "orders_submitted": 0}
        output = io.StringIO()
        with (patch("quantpaper.admission_runner.run_admission", return_value=report) as runner,
              patch("quantpaper.admission_runner.admission_summary", return_value=visible),
              redirect_stdout(output)):
            result = main(["admission", "--source-dir", "frozen", "--run-dir", "new-audit"])
        self.assertEqual(result, 0)
        runner.assert_called_once_with(Path("new-audit"), Path("frozen"))
        self.assertIn('"execution_enabled": false', output.getvalue())
        self.assertIn('"orders_submitted": 0', output.getvalue())


if __name__ == "__main__":
    unittest.main()
