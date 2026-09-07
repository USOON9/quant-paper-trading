"""Shared immutable fetch result and canonical hashing."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from typing import Generic, TypeVar


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class FetchBatch(Generic[T]):
    source: str
    request: dict[str, object]
    records: list[T]
    content_hash: str


def canonical_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def after_local_date(day: str, zone: str) -> str:
    """Date-only publications become usable after the provider's local day ends.

    This is an explicit conservative assumption, not a measured publication time.
    Using UTC midnight would expose a late US release several hours too early.
    """
    next_day = date.fromisoformat(day) + timedelta(days=1)
    return datetime.combine(next_day, time.min, ZoneInfo(zone)).astimezone(
        timezone.utc
    ).isoformat().replace("+00:00", "Z")
