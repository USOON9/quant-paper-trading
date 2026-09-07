"""Append-only, hash-chained JSONL audit trail."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


@dataclass(slots=True)
class AuditTrail:
    path: Path
    _previous_hash: str = field(default="0" * 64, init=False)

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            self._previous_hash = self._verify(file)

    @staticmethod
    def _verify(file: Any) -> str:
        """Validate every record; a truncated write must never become trusted history.

        This detects accidental corruption and edits without recomputing the chain.
        It is not tamper-proof: external signing/anchoring is required for that.
        """
        file.seek(0)
        previous_hash = "0" * 64
        for line_number, line in enumerate(file, 1):
            try:
                if not line.endswith("\n"):
                    raise ValueError("incomplete record")
                payload = json.loads(line)
                digest = payload.pop("hash")
                if payload["previous_hash"] != previous_hash:
                    raise ValueError("broken link")
                canonical = json.dumps(
                    payload, sort_keys=True, separators=(",", ":"), default=_json_default
                )
                expected = hashlib.sha256(canonical.encode()).hexdigest()
                if digest != expected:
                    raise ValueError("hash mismatch")
                previous_hash = digest
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                raise ValueError(f"audit corruption at line {line_number}") from error
        return previous_hash

    def record(self, event: str, ts_ns: int, data: dict[str, Any]) -> None:
        # Re-read under the same lock as append: multiple instances/processes
        # cannot rely on a cached previous hash.
        with self.path.open("a+", encoding="utf-8") as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            previous_hash = self._verify(file)
            payload = {"event": event, "ts_ns": ts_ns, "data": data, "previous_hash": previous_hash}
            canonical = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), default=_json_default
            )
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            payload["hash"] = digest
            file.write(json.dumps(payload, separators=(",", ":"), default=_json_default) + "\n")
            file.flush()
            os.fsync(file.fileno())
        self._previous_hash = digest
