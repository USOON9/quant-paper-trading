"""Bounded current FRED series metadata, observed locally without backdating.

Metadata describes units/frequency; it is neither an observation vintage nor
proof that today's description was known on a historical observation date.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
import json
import re
from urllib.parse import urlencode

from .common import FetchBatch, canonical_hash
from .http import SourceHTTPError, get_json as bounded_get_json


FRED_METADATA_URL = "https://api.stlouisfed.org/fred/series"
SOURCE = "fred-series-metadata"
AVAILABILITY_BASIS = "local_response_observed_metadata"
MAX_RESPONSE_BYTES = 1_000_000
_HTTP_CATEGORIES = frozenset({
    "invalid_request", "invalid_url", "redirect", "http_status", "invalid_response",
    "response_too_large", "tls_configuration", "tls_verification", "tls_error", "transport",
})
_SERIES_ID = re.compile(r"[A-Z0-9_]{1,64}\Z", re.ASCII)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}(?::?[0-9]{2})?)\Z", re.ASCII,
)
_TEXT_LIMITS = {
    "title": 1024, "frequency": 256, "frequency_short": 64,
    "units": 512, "units_short": 128,
    "seasonal_adjustment": 256, "seasonal_adjustment_short": 64,
}
_DATE_FIELDS = ("observation_start", "observation_end", "realtime_start", "realtime_end")
_RECORD_FIELDS = frozenset({
    "series_id", *_TEXT_LIMITS, *_DATE_FIELDS, "last_updated", "observed_at",
    "available_at", "source", "source_url", "availability_basis",
})


def _default_get_json(url: str) -> object:
    return bounded_get_json(
        url, {"User-Agent": "quant-paper-research/0.1"},
        allowed_hosts=frozenset({"api.stlouisfed.org"}), max_bytes=MAX_RESPONSE_BYTES,
    )


def _series_id(value: object, *, normalize: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("Invalid FRED metadata series ID")
    clean = value.strip().upper() if normalize else value
    if not _SERIES_ID.fullmatch(clean):
        raise ValueError("Invalid FRED metadata series ID")
    return clean


def _date(value: object) -> date:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError("Invalid FRED metadata date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("Invalid FRED metadata date") from None


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ValueError("Invalid FRED metadata timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone required")
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Invalid FRED metadata timestamp") from None


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _clock(now: Callable[[], datetime]) -> datetime:
    try:
        value = now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("aware clock required")
        return value.astimezone(timezone.utc)
    except Exception:
        raise ValueError("Invalid FRED metadata observation clock") from None


def validate_metadata_record(record: object) -> dict[str, object]:
    """Validate a persisted normalized metadata record, returning a fresh copy.

    Exact fields prevent arbitrary provider notes or URLs entering the catalog.
    Local availability is the observation time, not the provider update time.
    """
    if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
        raise ValueError("Invalid FRED metadata record fields")
    clean = _series_id(record["series_id"])
    for field, maximum in _TEXT_LIMITS.items():
        value = record[field]
        if (not isinstance(value, str) or not value.strip() or len(value) > maximum
                or any(ord(character) < 32 or ord(character) == 127 for character in value)):
            raise ValueError("Invalid FRED metadata text field")
    dates = {field: _date(record[field]) for field in _DATE_FIELDS}
    if (dates["observation_start"] > dates["observation_end"]
            or dates["realtime_start"] != dates["realtime_end"]):
        raise ValueError("Invalid FRED metadata date interval")
    timestamps = {field: _timestamp(record[field])
                  for field in ("last_updated", "observed_at", "available_at")}
    if any(record[field] != _iso(value) for field, value in timestamps.items()):
        raise ValueError("FRED metadata timestamps must be normalized UTC")
    if timestamps["available_at"] != timestamps["observed_at"]:
        raise ValueError("FRED metadata availability must equal local observation time")
    if (timestamps["last_updated"] > timestamps["observed_at"]
            or dates["realtime_end"] > timestamps["observed_at"].date()):
        raise ValueError("FRED metadata exceeds its observation time")
    if (record["source"] != SOURCE or record["availability_basis"] != AVAILABILITY_BASIS
            or record["source_url"] != f"https://fred.stlouisfed.org/series/{clean}"):
        raise ValueError("Invalid FRED metadata provenance")
    return dict(record)


def _check_json(value: object, depth: int = 0) -> None:
    """Reject injected non-JSON values as strictly as the bounded transport."""
    if depth > 64:
        raise ValueError("Invalid FRED metadata JSON payload")
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Invalid FRED metadata JSON payload")
        for item in value.values():
            _check_json(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_json(item, depth + 1)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise ValueError("Invalid FRED metadata JSON payload")


class FREDSeriesMetadataClient:
    def __init__(
        self, api_key: str, get_json: Callable[[str], object] = _default_get_json,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not re.fullmatch(r"[a-z0-9]{32}", api_key.strip(), re.ASCII):
            raise ValueError("FRED_API_KEY is missing or invalid")
        self._api_key = api_key.strip()
        self._get_json = get_json
        self._now = now if now is not None else lambda: datetime.now(timezone.utc)

    def fetch(self, series_id: str) -> FetchBatch[dict]:
        clean = _series_id(series_id, normalize=True)
        started = _clock(self._now)
        request = {"series_id": clean, "file_type": "json",
                   "realtime_start": started.date().isoformat(),
                   "realtime_end": started.date().isoformat()}
        try:
            payload = self._get_json(
                f"{FRED_METADATA_URL}?{urlencode({**request, 'api_key': self._api_key})}"
            )
        except SourceHTTPError as error:
            # Keep recognized transport categories, never an injected URL or
            # an original chained exception containing the credentialed URL.
            category = error.category
            if not isinstance(category, str) or category not in _HTTP_CATEGORIES:
                category = "transport"
            raise SourceHTTPError(category) from None
        except Exception:
            raise RuntimeError("FRED metadata request failed") from None
        observed = _clock(self._now)
        if observed < started:
            raise ValueError("FRED metadata observation clock moved backward")
        if not isinstance(payload, dict) or "error_code" in payload:
            raise ValueError("Invalid FRED metadata response")
        try:
            _check_json(payload)
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if len(encoded.encode("utf-8")) > MAX_RESPONSE_BYTES:
                raise ValueError("payload limit")
        except Exception:
            raise ValueError("Invalid or oversized FRED metadata JSON payload") from None
        entries = payload.get("seriess")
        if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
            raise ValueError("FRED metadata must contain exactly one series")
        entry = entries[0]
        if entry.get("id") != clean:
            raise ValueError("FRED metadata response series does not match request")
        for field in ("realtime_start", "realtime_end"):
            if (payload.get(field) != request[field] or entry.get(field) != request[field]):
                raise ValueError("FRED metadata real-time dates do not match request")
            _date(payload[field])
        record = {field: entry.get(field) for field in (*_TEXT_LIMITS, *_DATE_FIELDS)}
        record.update({
            "series_id": clean,
            "last_updated": _iso(_timestamp(entry.get("last_updated"))),
            "observed_at": _iso(observed), "available_at": _iso(observed),
            "source": SOURCE, "source_url": f"https://fred.stlouisfed.org/series/{clean}",
            "availability_basis": AVAILABILITY_BASIS,
        })
        normalized = validate_metadata_record(record)
        return FetchBatch(source=SOURCE, request=request, records=[normalized],
                          content_hash=canonical_hash(payload))
