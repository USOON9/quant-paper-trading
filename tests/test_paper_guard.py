"""Offline persistence and failure-path tests for the account-scoped paper guard."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from quantpaper import paper_guard
from quantpaper.paper_guard import PaperGuardError, PaperSessionGuard


ACCOUNT = "ab345678-1234-5678-9234-567812345678"
OTHER_ACCOUNT = "bb345678-1234-5678-9234-567812345678"
NOW = datetime(2026, 9, 8, 14, tzinfo=timezone.utc)


def result_for(run_id):
    return {"entry_order_id": str(uuid5(NAMESPACE_URL, run_id + "/entry")),
            "exit_order_id": str(uuid5(NAMESPACE_URL, run_id + "/exit")),
            "filled_qty": "0.01", "entry_price": "500.01", "exit_price": "500.02"}


class PaperGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.directory = self.root / "paper-state"
        self.guard = PaperSessionGuard(self.directory, ACCOUNT)

    def operation(self, guard=None, run_id="run-1", now=NOW, **kwargs):
        return (guard or self.guard).operation(run_id=run_id, symbol=kwargs.get("symbol", "SPY"),
                                               notional=kwargs.get("notional", Decimal("5")), now=now)

    def complete(self, run_id="run-1", now=NOW, guard=None):
        with self.operation(guard=guard, run_id=run_id, now=now) as ticket:
            ticket.complete(result_for(run_id))
        return ticket

    def state(self):
        return json.loads((self.directory / self.guard._state_name).read_text())

    def write_state(self, state):
        self.directory.mkdir(exist_ok=True)
        (self.directory / self.guard._state_name).write_text(json.dumps(state))

    def assert_blocked_operation(self, code, **kwargs):
        with self.assertRaises(PaperGuardError) as caught:
            with self.operation(**kwargs):
                self.fail("Blocked operation yielded permission")
        self.assertEqual(caught.exception.code, code)

    def test_absent_inspection_does_not_create_any_file(self):
        self.assertEqual(self.guard.inspect(now=NOW), {"blocked": False, "reasons": [], "attempts_today": 0})
        self.assertFalse(self.directory.exists())
        self.directory.mkdir()
        self.assertFalse(self.guard.inspect(now=NOW)["blocked"])
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_reservation_precedes_yield_and_acknowledgment_precedes_completion(self):
        with self.operation() as ticket:
            reservation = self.state()
            self.assertEqual(reservation["runs"][0]["status"], "RECONCILIATION_REQUIRED")
            self.assertIsNone(reservation["runs"][0]["result"])
            self.assertEqual(reservation["runs"][0]["reserved_at"], "2026-09-08T14:00:00.000000+00:00")
            self.assertEqual(reservation["runs"][0]["notional_usd"], "5.00")
            self.assertEqual(len(ticket.entry_client_order_id), 45)
            self.assertNotEqual(ticket.entry_client_order_id, ticket.exit_client_order_id)
            ticket.complete(result_for("run-1"))
            self.assertEqual(self.state(), reservation)
        self.assertEqual(self.state()["runs"][0]["status"], "COMPLETE")
        self.assertEqual(self.state()["runs"][0]["result"], result_for("run-1"))
        self.assertEqual(self.guard.inspect(now=NOW),
                         {"blocked": True, "reasons": ["DAILY_LIMIT_REACHED"], "attempts_today": 1})

    def test_same_account_normalization_and_different_account_namespace(self):
        other_instance = PaperSessionGuard(self.directory, ACCOUNT.upper())
        self.assertEqual(self.guard._ids("run-1"), other_instance._ids("run-1"))
        self.complete()
        self.assert_blocked_operation("DAILY_LIMIT_REACHED", guard=other_instance, run_id="different-run")
        other_account = PaperSessionGuard(self.directory, OTHER_ACCOUNT)
        self.assertNotEqual(self.guard._ids("run-1"), other_account._ids("run-1"))
        self.complete(run_id="run-1", guard=other_account)
        self.assertEqual(len(list(self.directory.glob("*.json"))), 2)

    def test_daily_capacity_returns_next_day_but_run_id_can_never_be_reused(self):
        self.complete()
        tomorrow = NOW + timedelta(days=1)
        self.assertEqual(self.guard.inspect(now=tomorrow), {"blocked": False, "reasons": [], "attempts_today": 0})
        self.assert_blocked_operation("RUN_ID_REUSED", now=tomorrow)
        self.complete(run_id="run-2", now=tomorrow)
        self.assertEqual(len(self.state()["runs"]), 2)

    def test_daily_quota_uses_new_york_not_utc_date_and_handles_dst(self):
        first = datetime(2026, 11, 1, 3, 59, tzinfo=timezone.utc)
        self.complete(now=first)
        self.assertEqual(self.state()["runs"][0]["trading_date"], "2026-10-31")
        second = datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)
        self.complete(run_id="run-2", now=second)
        self.assert_blocked_operation("DAILY_LIMIT_REACHED", run_id="run-3",
                                      now=datetime(2026, 11, 2, 4, 59, tzinfo=timezone.utc))
        self.complete(run_id="run-3", now=datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc))
        self.assertEqual([row["trading_date"] for row in self.state()["runs"]],
                         ["2026-10-31", "2026-11-01", "2026-11-02"])

    def test_unmarked_exit_is_an_error_and_blocks_all_future_dates(self):
        with self.assertRaises(PaperGuardError) as caught:
            with self.operation():
                pass
        self.assertEqual(caught.exception.code, "RECONCILIATION_REQUIRED")
        self.assertEqual(self.guard.inspect(now=NOW + timedelta(days=3)),
                         {"blocked": True, "reasons": ["RECONCILIATION_REQUIRED"], "attempts_today": 0})
        self.assert_blocked_operation("RECONCILIATION_REQUIRED", run_id="new", now=NOW + timedelta(days=3))

    def test_exceptions_before_or_after_acknowledgment_leave_reservation(self):
        for acknowledge in (False, True):
            with self.subTest(acknowledge=acknowledge):
                guard = PaperSessionGuard(self.root / str(acknowledge), ACCOUNT)
                with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                    with self.operation(guard=guard) as ticket:
                        if acknowledge:
                            ticket.complete(result_for("run-1"))
                        raise RuntimeError("synthetic failure")
                state = json.loads((guard.state_directory / guard._state_name).read_text())
                self.assertEqual(state["runs"][0]["status"], "RECONCILIATION_REQUIRED")
                self.assertIsNone(state["runs"][0]["result"])
                self.assertIn("RECONCILIATION_REQUIRED", guard.inspect(now=NOW)["reasons"])

    def test_interrupt_leaves_durable_reservation_and_releases_lock(self):
        with self.assertRaises(KeyboardInterrupt):
            with self.operation():
                raise KeyboardInterrupt()
        self.assertNotIn("OPERATION_IN_PROGRESS", self.guard.inspect(now=NOW)["reasons"])
        self.assertIn("RECONCILIATION_REQUIRED", self.guard.inspect(now=NOW)["reasons"])

    def test_ticket_cannot_acknowledge_twice_or_after_context_closes(self):
        with self.operation() as ticket:
            ticket.complete(result_for("run-1"))
            with self.assertRaises(PaperGuardError) as caught:
                ticket.complete(result_for("run-1"))
            self.assertEqual(caught.exception.code, "TICKET_ALREADY_COMPLETED")
        with self.assertRaises(PaperGuardError) as caught:
            ticket.complete(result_for("run-1"))
        self.assertEqual(caught.exception.code, "TICKET_CLOSED")

    def test_completion_requires_only_safe_exact_fields_and_valid_decimal_strings(self):
        bad_results = [None, {}, {**result_for("r"), "message": "private provider message"}]
        for key, values in {"entry_order_id": ["not-uuid", None],
                            "filled_qty": [0.01, "0", "NaN", "Infinity", "1e-2", "-1", ".1", "01.0"],
                            "entry_price": ["0", "1" * 65], "exit_price": [True, " 1"]}.items():
            bad_results.extend({**result_for("r"), key: value} for value in values)
        bad_results.append({**result_for("r"), "exit_order_id": result_for("r")["entry_order_id"]})
        with self.operation() as ticket:
            for result in bad_results:
                with self.subTest(result=result), self.assertRaises(PaperGuardError) as caught:
                    ticket.complete(result)
                self.assertEqual(caught.exception.code, "INVALID_COMPLETION")
            ticket.complete(result_for("run-1"))
        self.assertNotIn("private provider message", json.dumps(self.state()))

    def test_invalid_operation_inputs_never_create_state(self):
        inputs = [({"run_id": value}, "INVALID_RUN_ID") for value in
                  ("", "_first", "../run", "hello world", "\u6c49\u5b57", "x" * 65, True, None)]
        inputs += [({"symbol": value}, "UNSUPPORTED_SYMBOL") for value in ("BTC/USD", "spy", None, True)]
        inputs += [({"notional": value}, "INVALID_NOTIONAL") for value in
                   (5, 5.0, True, "5", Decimal("0.99"), Decimal("25.01"), Decimal("1.001"),
                    Decimal("NaN"), Decimal("Infinity"))]
        inputs += [({"now": value}, "INVALID_CLOCK") for value in (None, NOW.replace(tzinfo=None), "2026-09-08")]
        for kwargs, code in inputs:
            with self.subTest(kwargs=kwargs):
                self.assert_blocked_operation(code, **kwargs)
                self.assertFalse(self.directory.exists())

    def test_supported_notional_boundaries_and_longest_run_id(self):
        for index, value in enumerate((Decimal("1"), Decimal("25"), Decimal("5.00"))):
            with self.operation(run_id=(str(index) + "a" * 63), now=NOW + timedelta(days=index), notional=value) as ticket:
                ticket.complete(result_for(str(index)))
        self.assertEqual([row["notional_usd"] for row in self.state()["runs"]], ["1.00", "25.00", "5.00"])

    def test_constructor_rejects_unsafe_directory_and_account_id(self):
        for path in (str(self.directory), Path("/"), self.directory / ".." / "elsewhere"):
            with self.subTest(path=path), self.assertRaises(PaperGuardError):
                PaperSessionGuard(path, ACCOUNT)
        for account in ("", "secret-key", ACCOUNT.replace("-", ""), None, True):
            with self.subTest(account=account), self.assertRaises(PaperGuardError) as caught:
                PaperSessionGuard(self.directory, account)
            self.assertEqual(caught.exception.code, "INVALID_IDENTIFIER")

    def test_clock_rollback_is_blocked_even_after_successful_completion(self):
        self.complete()
        prior = NOW - timedelta(days=1)
        self.assertEqual(self.guard.inspect(now=prior),
                         {"blocked": True, "reasons": ["CLOCK_ROLLBACK"], "attempts_today": 0})
        self.assert_blocked_operation("CLOCK_ROLLBACK", run_id="earlier", now=prior)

    def test_state_is_checked_again_between_inspection_and_operation(self):
        self.assertFalse(self.guard.inspect(now=NOW)["blocked"])
        self.directory.mkdir()
        (self.directory / self.guard._state_name).write_text("not json")
        self.assert_blocked_operation("STATE_INVALID")
        self.assertEqual(self.guard.inspect(now=NOW),
                         {"blocked": True, "reasons": ["STATE_INVALID"], "attempts_today": None})

    def test_corrupt_checksum_account_schema_and_duplicate_json_keys_fail_closed(self):
        self.complete()
        original = self.state()
        malformed = ["{", '{"schema_version":1,"schema_version":1}', '{"bad":NaN}', "[]"]
        for mutate in (lambda s: s.update(state_hash="0" * 64),
                       lambda s: s.update(account_hash="0" * 64),
                       lambda s: s.update(schema_version=True),
                       lambda s: s.update(extra="unrecognized")):
            state = deepcopy(original)
            mutate(state)
            malformed.append(json.dumps(state))
        for text in malformed:
            with self.subTest(text=text[:80]):
                (self.directory / self.guard._state_name).write_text(text)
                self.assertIn("STATE_INVALID", self.guard.inspect(now=NOW)["reasons"])
                self.assert_blocked_operation("STATE_INVALID", run_id="new", now=NOW + timedelta(days=1))

    def test_rehashed_semantic_corruption_is_rejected(self):
        self.complete()
        original = self.state()["runs"]
        changes = {"status": "PENDING", "symbol": "QQQ", "notional_usd": "5", "trading_date": "2026-09-09",
                   "reserved_at": "2026-09-08T14:00:00Z", "entry_client_order_id": "wrong"}
        for key, value in changes.items():
            with self.subTest(key=key):
                rows = deepcopy(original)
                rows[0][key] = value
                self.write_state(self.guard._seal(rows))
                self.assertIn("STATE_INVALID", self.guard.inspect(now=NOW)["reasons"])
        rows = deepcopy(original)
        rows.append(deepcopy(rows[0]))
        self.write_state(self.guard._seal(rows))
        self.assertIn("STATE_INVALID", self.guard.inspect(now=NOW)["reasons"])

    def test_foreign_account_state_cannot_be_rebound_by_renaming_file(self):
        self.complete()
        foreign = PaperSessionGuard(self.directory, OTHER_ACCOUNT)
        (self.directory / foreign._state_name).write_bytes((self.directory / self.guard._state_name).read_bytes())
        self.assertEqual(foreign.inspect(now=NOW)["reasons"], ["STATE_INVALID"])
        self.assert_blocked_operation("STATE_INVALID", guard=foreign)

    def test_oversized_state_is_rejected_before_parsing(self):
        self.directory.mkdir()
        with patch.object(paper_guard, "MAX_STATE_BYTES", 100):
            (self.directory / self.guard._state_name).write_bytes(b" " * 101)
            self.assertEqual(self.guard.inspect(now=NOW)["reasons"], ["STATE_INVALID"])

    def test_symlink_ancestors_directory_state_and_lock_are_never_followed(self):
        real = self.root / "real"
        real.mkdir()
        link = self.root / "linked"
        link.symlink_to(real, target_is_directory=True)
        for location in (link, link / "child"):
            guard = PaperSessionGuard(location, ACCOUNT)
            self.assertEqual(guard.inspect(now=NOW)["reasons"], ["UNSAFE_STATE_PATH"])
            self.assert_blocked_operation("UNSAFE_STATE_PATH", guard=guard)
        self.assertEqual(list(real.iterdir()), [])
        target = self.root / "unrelated"
        target.write_text("preserve")
        self.directory.mkdir()
        for name, reason in ((self.guard._state_name, "STATE_INVALID"), (self.guard._lock_name, "LOCK_UNAVAILABLE")):
            with self.subTest(name=name):
                candidate = self.directory / name
                if candidate.exists():
                    candidate.unlink()
                candidate.symlink_to(target)
                self.assertIn(reason, self.guard.inspect(now=NOW)["reasons"])
                self.assert_blocked_operation(reason)
                self.assertEqual(target.read_text(), "preserve")
                candidate.unlink()

    def test_regular_file_in_directory_path_fails_closed(self):
        self.directory.write_text("unrelated")
        self.assertEqual(self.guard.inspect(now=NOW)["reasons"], ["UNSAFE_STATE_PATH"])
        self.assert_blocked_operation("UNSAFE_STATE_PATH")
        self.assertEqual(self.directory.read_text(), "unrelated")

    def test_exclusive_lock_is_held_until_context_exits(self):
        competitor = PaperSessionGuard(self.directory, ACCOUNT)
        with self.operation() as ticket:
            self.assert_blocked_operation("OPERATION_IN_PROGRESS", guard=competitor, run_id="competitor")
            observed = competitor.inspect(now=NOW)
            self.assertEqual(observed["attempts_today"], 1)
            self.assertIn("OPERATION_IN_PROGRESS", observed["reasons"])
            ticket.complete(result_for("run-1"))
            self.assert_blocked_operation("OPERATION_IN_PROGRESS", guard=competitor, run_id="competitor")
        self.assertNotIn("OPERATION_IN_PROGRESS", competitor.inspect(now=NOW)["reasons"])

    def test_separate_process_cannot_take_account_lock(self):
        script = """from datetime import datetime
from decimal import Decimal
from pathlib import Path
import sys
from quantpaper.paper_guard import PaperSessionGuard, PaperGuardError
try:
    with PaperSessionGuard(Path(sys.argv[1]), sys.argv[2]).operation(run_id='competing', symbol='SPY', notional=Decimal('5'), now=datetime.fromisoformat(sys.argv[3])):
        raise AssertionError('permission yielded')
except PaperGuardError as exc:
    print(exc.code)
"""
        with self.operation() as ticket:
            child = subprocess.run([sys.executable, "-c", script, str(self.directory), ACCOUNT, NOW.isoformat()],
                                   capture_output=True, text=True, timeout=10, check=True)
            self.assertEqual(child.stdout.strip(), "OPERATION_IN_PROGRESS")
            self.assertEqual(child.stderr, "")
            ticket.complete(result_for("run-1"))

    def test_reservation_write_failure_never_yields_permission(self):
        with patch.object(self.guard, "_atomic_write", side_effect=OSError("private disk failure")):
            self.assert_blocked_operation("STATE_WRITE_FAILED")
        self.assertFalse((self.directory / self.guard._state_name).exists())
        self.assertFalse(any(path.suffix == ".tmp" for path in self.directory.iterdir()))

    def test_reservation_rename_failure_cleans_temporary_file_and_never_yields(self):
        with patch.object(paper_guard.os, "replace", side_effect=OSError("private rename failure")):
            self.assert_blocked_operation("STATE_WRITE_FAILED")
        self.assertFalse((self.directory / self.guard._state_name).exists())
        self.assertEqual([path.suffix for path in self.directory.iterdir()], [".lock"])

    def test_completion_failure_after_replacement_restores_pending_reservation(self):
        original = self.guard._atomic_write

        def fail_completed_after_write(directory_fd, payload):
            original(directory_fd, payload)
            if json.loads(payload)["runs"][-1]["status"] == "COMPLETE":
                raise OSError("completion directory sync failed")

        with patch.object(self.guard, "_atomic_write", side_effect=fail_completed_after_write):
            with self.assertRaises(PaperGuardError) as caught:
                self.complete()
        self.assertEqual(caught.exception.code, "STATE_WRITE_FAILED")
        self.assertEqual(self.state()["runs"][0]["status"], "RECONCILIATION_REQUIRED")
        self.assert_blocked_operation("RECONCILIATION_REQUIRED", run_id="new", now=NOW + timedelta(days=1))

    def test_failed_completion_and_failed_atomic_restore_invalidate_ledger(self):
        original = self.guard._atomic_write
        calls = 0

        def fail_after_reservation(directory_fd, payload):
            nonlocal calls
            calls += 1
            if calls == 1:
                return original(directory_fd, payload)
            if calls == 2:
                original(directory_fd, payload)
            raise OSError("persistent atomic writer failure")

        with patch.object(self.guard, "_atomic_write", side_effect=fail_after_reservation):
            with self.assertRaises(PaperGuardError) as caught:
                self.complete()
        self.assertEqual(caught.exception.code, "STATE_WRITE_FAILED")
        self.assertEqual(self.guard.inspect(now=NOW)["reasons"], ["STATE_INVALID"])
        self.assert_blocked_operation("STATE_INVALID", run_id="new", now=NOW + timedelta(days=1))

    def test_partial_writes_are_completed_and_both_file_and_directory_are_synced(self):
        write, fsync = os.write, os.fsync
        synced_types = []

        def short_write(descriptor, payload):
            return write(descriptor, payload[:13])

        def record_sync(descriptor):
            synced_types.append(stat.S_IFMT(os.fstat(descriptor).st_mode))
            return fsync(descriptor)

        with patch.object(paper_guard.os, "write", side_effect=short_write), patch.object(paper_guard.os, "fsync", side_effect=record_sync):
            self.complete()
        self.assertEqual(self.state()["runs"][0]["status"], "COMPLETE")
        self.assertGreaterEqual(synced_types.count(stat.S_IFREG), 3)
        self.assertGreaterEqual(synced_types.count(stat.S_IFDIR), 3)

    def test_lock_fsync_failure_prevents_permission_and_has_sanitized_error(self):
        self.directory.mkdir()
        with patch.object(paper_guard.os, "fsync", side_effect=OSError("private disk details")):
            with self.assertRaises(PaperGuardError) as caught:
                with self.operation():
                    self.fail("No permission on lock persistence failure")
        self.assertEqual(caught.exception.code, "STATE_UNAVAILABLE")
        self.assertNotIn("private", str(caught.exception))
        self.assertFalse((self.directory / self.guard._state_name).exists())

    def test_reused_broker_result_ids_cannot_mark_later_cycle_complete(self):
        self.complete()
        with self.assertRaises(PaperGuardError) as caught:
            with self.operation(run_id="run-2", now=NOW + timedelta(days=1)) as ticket:
                ticket.complete(result_for("run-1"))
        self.assertEqual(caught.exception.code, "STATE_WRITE_FAILED")
        self.assertEqual(self.state()["runs"][-1]["status"], "RECONCILIATION_REQUIRED")

    def test_capacity_is_bounded_without_pruning_existing_history(self):
        rows = []
        for index in range(paper_guard.MAX_RUNS):
            run_id = "run-" + str(index)
            when = NOW + timedelta(days=index)
            entry, exit_id = self.guard._ids(run_id)
            rows.append({"run_id": run_id, "symbol": "SPY", "notional_usd": "5.00",
                         "trading_date": when.astimezone(paper_guard.NEW_YORK).date().isoformat(),
                         "reserved_at": when.isoformat(timespec="microseconds"),
                         "entry_client_order_id": entry, "exit_client_order_id": exit_id,
                         "status": "COMPLETE", "result": result_for(run_id)})
        self.write_state(self.guard._seal(rows))
        before = (self.directory / self.guard._state_name).read_bytes()
        when = NOW + timedelta(days=paper_guard.MAX_RUNS)
        self.assertEqual(self.guard.inspect(now=when),
                         {"blocked": True, "reasons": ["STATE_CAPACITY_REACHED"], "attempts_today": 0})
        self.assert_blocked_operation("STATE_CAPACITY_REACHED", run_id="beyond-capacity", now=when)
        self.assertEqual((self.directory / self.guard._state_name).read_bytes(), before)
        self.assertEqual(len(self.state()["runs"]), 1000)

    def test_summary_does_not_expose_account_hash_identifiers_or_paths(self):
        self.complete()
        summary = json.dumps(self.guard.inspect(now=NOW))
        for private in (ACCOUNT, hashlib.sha256(ACCOUNT.encode()).hexdigest(), str(self.directory), "run-1",
                        result_for("run-1")["entry_order_id"]):
            self.assertNotIn(private, summary)
        self.assertEqual(set(json.loads(summary)), {"blocked", "reasons", "attempts_today"})


if __name__ == "__main__":
    unittest.main()
