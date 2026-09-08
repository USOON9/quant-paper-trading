"""Durable account-scoped reservation guard for one supervised SPY paper cycle.

This module does not contact a broker or validate fills. The service must verify
the account, execution results, and flatness before acknowledging a ticket.
The caller fixes the state directory; changing it defeats a local-file guard.
Checksums detect ordinary corruption, not coordinated ledger rewriting/deletion.
Reservation clocks are caller-supplied audit clocks, never fill timestamps.
Filesystem failure can prevent any durable write; detected failures never yield
permission and completion failures restore or invalidate the unresolved ledger.
"""

from __future__ import annotations

from contextlib import contextmanager, suppress
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo


MAX_RUNS = 1000
MAX_STATE_BYTES = 2_000_000
NEW_YORK = ZoneInfo("America/New_York")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z", re.ASCII)
PLAIN_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z", re.ASCII)
RUN_FIELDS = frozenset({"run_id", "symbol", "notional_usd", "trading_date", "reserved_at",
                        "entry_client_order_id", "exit_client_order_id", "status", "result"})
RESULT_FIELDS = frozenset({"entry_order_id", "exit_order_id", "filled_qty", "entry_price", "exit_price"})


class PaperGuardError(RuntimeError):
    """A fixed reason code without identifiers, paths, or provider messages."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"Paper session guard blocked ({code})")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _uuid(value: object) -> str:
    try:
        if not isinstance(value, str) or len(value) != 36:
            raise ValueError()
        normalized = str(UUID(value))
        if normalized != value.lower():
            raise ValueError()
        return normalized
    except (TypeError, ValueError, AttributeError):
        raise PaperGuardError("INVALID_IDENTIFIER") from None


def _clock(value: object) -> datetime:
    try:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError()
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise PaperGuardError("INVALID_CLOCK") from None


def _time_text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _stored_clock(value: object) -> datetime:
    try:
        if not isinstance(value, str):
            raise ValueError()
        result = _clock(datetime.fromisoformat(value))
        if _time_text(result) != value:
            raise ValueError()
        return result
    except (ValueError, PaperGuardError):
        raise PaperGuardError("STATE_INVALID") from None


def _notional(value: object) -> str:
    try:
        if not isinstance(value, Decimal) or not value.is_finite() or not Decimal("1") <= value <= Decimal("25"):
            raise ValueError()
        if value != value.quantize(Decimal("0.01")):
            raise ValueError()
        return format(value, ".2f")
    except (ValueError, InvalidOperation):
        raise PaperGuardError("INVALID_NOTIONAL") from None


def _result(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != RESULT_FIELDS:
        raise PaperGuardError("INVALID_COMPLETION")
    try:
        result = {name: _uuid(value[name]) for name in ("entry_order_id", "exit_order_id")}
        if result["entry_order_id"] == result["exit_order_id"]:
            raise ValueError()
        for name in ("filled_qty", "entry_price", "exit_price"):
            text = value[name]
            if not isinstance(text, str) or len(text) > 64 or not PLAIN_DECIMAL.fullmatch(text):
                raise ValueError()
            number = Decimal(text)
            if not number.is_finite() or number <= 0:
                raise ValueError()
            result[name] = text
        return result
    except (ValueError, InvalidOperation, PaperGuardError):
        raise PaperGuardError("INVALID_COMPLETION") from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("nonfinite constant")


class PaperTicket:
    """An active reservation; complete is an explicit service assertion, not a fill check."""

    def __init__(self, entry_client_order_id: str, exit_client_order_id: str):
        self._entry_client_order_id = entry_client_order_id
        self._exit_client_order_id = exit_client_order_id
        self._completion = None
        self._active = True

    @property
    def entry_client_order_id(self) -> str:
        return self._entry_client_order_id

    @property
    def exit_client_order_id(self) -> str:
        return self._exit_client_order_id

    def complete(self, result: dict) -> None:
        if not self._active:
            raise PaperGuardError("TICKET_CLOSED")
        if self._completion is not None:
            raise PaperGuardError("TICKET_ALREADY_COMPLETED")
        self._completion = _result(result)


class PaperSessionGuard:
    def __init__(self, state_directory: Path, account_id: str):
        if not isinstance(state_directory, Path) or ".." in state_directory.parts:
            raise PaperGuardError("UNSAFE_STATE_PATH")
        directory = state_directory.absolute()
        if directory == Path(directory.anchor):
            raise PaperGuardError("UNSAFE_STATE_PATH")
        self.state_directory = directory
        self._account_hash = hashlib.sha256(_uuid(account_id).encode("ascii")).hexdigest()
        self._state_name = self._account_hash + ".json"
        self._lock_name = self._account_hash + ".lock"

    def _directory_fd(self, *, create: bool) -> int | None:
        """Walk each component through directory descriptors without following links."""
        descriptor = os.open(self.state_directory.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in self.state_directory.parts[1:]:
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        os.close(descriptor)
                        return None
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                        os.fsync(descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return descriptor
        except OSError:
            with suppress(OSError):
                os.close(descriptor)
            raise PaperGuardError("UNSAFE_STATE_PATH") from None

    def _ids(self, run_id: str) -> tuple[str, str]:
        return tuple("qps1-" + hashlib.sha256(
            (self._account_hash + "\0" + run_id + "\0" + leg).encode("ascii")
        ).hexdigest()[:40] for leg in ("entry", "exit"))

    def _seal(self, runs: list[dict]) -> dict:
        value = {"schema_version": 1, "account_hash": self._account_hash, "runs": runs}
        return {**value, "state_hash": _digest(value)}

    def _validate_state(self, state: object) -> dict:
        try:
            if (not isinstance(state, dict) or set(state) != {"schema_version", "account_hash", "runs", "state_hash"}
                    or type(state["schema_version"]) is not int or state["schema_version"] != 1
                    or state["account_hash"] != self._account_hash
                    or state["state_hash"] != _digest({key: value for key, value in state.items() if key != "state_hash"})
                    or not isinstance(state["runs"], list) or len(state["runs"]) > MAX_RUNS):
                raise ValueError()
            run_ids, days, order_ids = set(), set(), set()
            previous = None
            for index, row in enumerate(state["runs"]):
                if not isinstance(row, dict) or set(row) != RUN_FIELDS:
                    raise ValueError()
                run_id = row["run_id"]
                if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id) or run_id in run_ids or row["symbol"] != "SPY":
                    raise ValueError()
                if not isinstance(row["notional_usd"], str) or _notional(Decimal(row["notional_usd"])) != row["notional_usd"]:
                    raise ValueError()
                when = _stored_clock(row["reserved_at"])
                day = when.astimezone(NEW_YORK).date().isoformat()
                if row["trading_date"] != day or day in days or (previous is not None and when < previous):
                    raise ValueError()
                if (row["entry_client_order_id"], row["exit_client_order_id"]) != self._ids(run_id):
                    raise ValueError()
                if row["status"] == "COMPLETE":
                    result = _result(row["result"])
                    if _canonical(result) != _canonical(row["result"]):
                        raise ValueError()
                    for name in ("entry_order_id", "exit_order_id"):
                        if result[name] in order_ids:
                            raise ValueError()
                        order_ids.add(result[name])
                elif row["status"] == "RECONCILIATION_REQUIRED":
                    if row["result"] is not None or index != len(state["runs"]) - 1:
                        raise ValueError()
                else:
                    raise ValueError()
                run_ids.add(run_id)
                days.add(day)
                previous = when
            return state
        except (KeyError, TypeError, ValueError, InvalidOperation, PaperGuardError):
            raise PaperGuardError("STATE_INVALID") from None

    def _read_state(self, directory_fd: int) -> dict:
        try:
            descriptor = os.open(self._state_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        except FileNotFoundError:
            return self._seal([])
        except OSError:
            raise PaperGuardError("STATE_INVALID") from None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATE_BYTES:
                raise ValueError()
            pieces, total = [], 0
            while True:
                data = os.read(descriptor, min(65536, MAX_STATE_BYTES + 1 - total))
                if not data:
                    break
                pieces.append(data)
                total += len(data)
                if total > MAX_STATE_BYTES:
                    raise ValueError()
            state = json.loads(b"".join(pieces), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            return self._validate_state(state)
        except (OSError, ValueError, TypeError, RecursionError):
            raise PaperGuardError("STATE_INVALID") from None
        finally:
            os.close(descriptor)

    def _atomic_write(self, directory_fd: int, payload: bytes) -> None:
        temporary = "." + self._account_hash + "." + uuid4().hex + ".tmp"
        descriptor = None
        try:
            try:
                current = os.stat(self._state_name, dir_fd=directory_fd, follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode):
                    raise OSError("unsafe state type")
            except FileNotFoundError:
                pass
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory_fd)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("incomplete state write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, self._state_name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory_fd)

    def _write_state(self, directory_fd: int, state: dict) -> None:
        self._validate_state(state)
        payload = _canonical(state) + b"\n"
        if len(payload) > MAX_STATE_BYTES:
            raise PaperGuardError("STATE_CAPACITY_REACHED")
        try:
            self._atomic_write(directory_fd, payload)
        except OSError:
            raise PaperGuardError("STATE_WRITE_FAILED") from None

    def _restore_unresolved(self, directory_fd: int, reservation: dict) -> None:
        try:
            self._write_state(directory_fd, reservation)
            return
        except BaseException:
            # If normal atomic restoration cannot complete, leave a deliberately
            # invalid ledger. No future read treats it as an absent clean state.
            with suppress(BaseException):
                descriptor = os.open(self._state_name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                                     0o600, dir_fd=directory_fd)
                try:
                    os.write(descriptor, b'{"storage_failure":true}\n')
                    os.fsync(descriptor)
                    os.fsync(directory_fd)
                finally:
                    os.close(descriptor)

    def _lock(self, directory_fd: int, *, create: bool, exclusive: bool) -> int | None:
        flags = (os.O_RDWR | os.O_CREAT) if create else os.O_RDONLY
        try:
            descriptor = os.open(self._lock_name, flags | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        except OSError:
            raise PaperGuardError("LOCK_UNAVAILABLE") from None
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise PaperGuardError("LOCK_UNAVAILABLE")
            try:
                fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PaperGuardError("OPERATION_IN_PROGRESS") from None
            if create:
                os.fsync(descriptor)
                os.fsync(directory_fd)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _release(descriptor: int | None) -> None:
        if descriptor is not None:
            with suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            with suppress(OSError):
                os.close(descriptor)

    def _reasons(self, state: dict, now: datetime) -> tuple[list[str], int]:
        runs = state["runs"]
        today = now.astimezone(NEW_YORK).date().isoformat()
        attempts = sum(row["trading_date"] == today for row in runs)
        reasons = []
        if runs and now < _stored_clock(runs[-1]["reserved_at"]):
            reasons.append("CLOCK_ROLLBACK")
        if any(row["status"] != "COMPLETE" for row in runs):
            reasons.append("RECONCILIATION_REQUIRED")
        if attempts:
            reasons.append("DAILY_LIMIT_REACHED")
        if len(runs) >= MAX_RUNS:
            reasons.append("STATE_CAPACITY_REACHED")
        return reasons, attempts

    def inspect(self, *, now: datetime) -> dict:
        """Read-only advisory status; operation rechecks under its exclusive lock."""
        when = _clock(now)
        directory_fd = lock_fd = None
        try:
            directory_fd = self._directory_fd(create=False)
            if directory_fd is None:
                return {"blocked": False, "reasons": [], "attempts_today": 0}
            lock_reason = None
            try:
                lock_fd = self._lock(directory_fd, create=False, exclusive=False)
            except PaperGuardError as error:
                lock_reason = error.code
            state = self._read_state(directory_fd)
            reasons, attempts = self._reasons(state, when)
            if lock_reason:
                reasons.insert(0, lock_reason)
            return {"blocked": bool(reasons), "reasons": reasons, "attempts_today": attempts}
        except (PaperGuardError, OSError) as error:
            reason = error.code if isinstance(error, PaperGuardError) else "STATE_UNAVAILABLE"
            return {"blocked": True, "reasons": [reason], "attempts_today": None}
        finally:
            self._release(lock_fd)
            if directory_fd is not None:
                with suppress(OSError):
                    os.close(directory_fd)

    @contextmanager
    def operation(self, *, run_id: str, symbol: str, notional: Decimal, now: datetime):
        when = _clock(now)
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise PaperGuardError("INVALID_RUN_ID")
        if symbol != "SPY" or not isinstance(symbol, str):
            raise PaperGuardError("UNSUPPORTED_SYMBOL")
        amount = _notional(notional)
        directory_fd = lock_fd = None
        ticket = None
        try:
            directory_fd = self._directory_fd(create=True)
            lock_fd = self._lock(directory_fd, create=True, exclusive=True)
            state = self._read_state(directory_fd)
            reasons, _ = self._reasons(state, when)
            if any(row["run_id"] == run_id for row in state["runs"]):
                reasons.insert(0, "RUN_ID_REUSED")
            if reasons:
                raise PaperGuardError(reasons[0])
            entry, exit_id = self._ids(run_id)
            row = {"run_id": run_id, "symbol": symbol, "notional_usd": amount,
                   "trading_date": when.astimezone(NEW_YORK).date().isoformat(), "reserved_at": _time_text(when),
                   "entry_client_order_id": entry, "exit_client_order_id": exit_id,
                   "status": "RECONCILIATION_REQUIRED", "result": None}
            reservation = self._seal([*state["runs"], row])
            self._write_state(directory_fd, reservation)
            ticket = PaperTicket(entry, exit_id)
            yield ticket
            if ticket._completion is None:
                raise PaperGuardError("RECONCILIATION_REQUIRED")
            completed = deepcopy(reservation["runs"])
            completed[-1].update(status="COMPLETE", result=ticket._completion)
            try:
                self._write_state(directory_fd, self._seal(completed))
            except BaseException:
                self._restore_unresolved(directory_fd, reservation)
                raise PaperGuardError("STATE_WRITE_FAILED") from None
        except OSError:
            raise PaperGuardError("STATE_UNAVAILABLE") from None
        finally:
            if ticket is not None:
                ticket._active = False
            self._release(lock_fd)
            if directory_fd is not None:
                with suppress(OSError):
                    os.close(directory_fd)
