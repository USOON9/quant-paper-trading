"""Bounded GET-only Alpaca historical data collector.

Routes follow the Alpaca historical stock and US crypto bars/quotes APIs. The
collector preserves raw records, including a provider's inclusive end boundary;
consumers must apply their research interval as [start, end). Collection time
records when this process actually observed each response, not publication time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from threading import Lock
import time
from typing import Any
from urllib.parse import urlsplit

from dotenv import dotenv_values
import requests


_DATA_ORIGIN = "https://data.alpaca.markets"
_PATHS = frozenset({
    "/v2/stocks/bars", "/v2/stocks/quotes",
    "/v1beta3/crypto/us/bars", "/v1beta3/crypto/us/quotes",
})
_EQUITIES = frozenset({"SPY", "JPM", "XOM", "WMT", "JNJ"})
_TIMEOUT = (5.0, 20.0)
_PAGE_LIMIT = 10_000
_MAX_PAGES = 10


class MarketDataError(RuntimeError):
    """A sanitized failure containing no URL, headers, keys or response body."""

    def __init__(self, category: str, safe_message: str, *, http_status: int | None = None):
        self.category = category
        self.http_status = http_status
        self.safe_message = safe_message
        super().__init__(safe_message)

    def __repr__(self) -> str:
        return (f"MarketDataError(category={self.category!r}, http_status={self.http_status!r}, "
                f"safe_message={self.safe_message!r})")


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MarketDataError("validation", "Market-data windows require timezone-aware datetimes")
    return value.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _credentials(key_id: object, secret_key: object) -> tuple[str | None, str | None]:
    values = []
    for value in (key_id, secret_key):
        if value is None or value == "":
            values.append(None)
        elif (not isinstance(value, str) or not value.strip() or len(value) > 4096
              or not value.isascii() or not value.isprintable()):
            raise MarketDataError("configuration", "Local market-data credentials are malformed")
        else:
            values.append(value.strip())
    if (values[0] is None) != (values[1] is None):
        raise MarketDataError("configuration", "Both Alpaca key ID and secret key must be configured")
    return values[0], values[1]


class MarketDataClient:
    """Fixed-host market-data client. Anonymous access is limited to US crypto.

    Sessions may be injected for offline tests. Production sessions disable
    environment proxies/.netrc, redirect following, and automatic HTTP retries.
    No base URL or arbitrary request method is exposed.
    """

    def __init__(self, key_id: str | None = None, secret_key: str | None = None,
                 *, session: Any | None = None, min_request_interval_seconds: float = 0) -> None:
        if (type(min_request_interval_seconds) not in (int, float)
                or not math.isfinite(min_request_interval_seconds)
                or not 0 <= min_request_interval_seconds <= 5):
            raise MarketDataError("configuration", "Request pacing interval must be finite and between 0 and 5 seconds")
        self._key_id, self._secret_key = _credentials(key_id, secret_key)
        self._min_request_interval_seconds = float(min_request_interval_seconds)
        self._request_lock = Lock()
        self._next_request_at: float | None = None
        self._request_count = 0
        self._session = session if session is not None else requests.Session()
        if session is None:
            self._session.trust_env = False
            # Requests defaults to zero retries; pin it explicitly for 429 and
            # transport failures rather than retrying against a data allowance.
            self._session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))

    def __repr__(self) -> str:
        return f"MarketDataClient(authenticated={self._key_id is not None})"

    @property
    def request_count(self) -> int:
        """GET attempts, including transport/HTTP failures and pagination pages."""
        return self._request_count

    @classmethod
    def from_env(cls, env_path: Path, *, min_request_interval_seconds: float = 0) -> MarketDataClient:
        """Read only the two credentials; process env takes precedence, unmodified."""
        try:
            file_values = dotenv_values(Path(env_path), interpolate=False)
        except Exception:
            raise MarketDataError("configuration", "Cannot read the local market-data environment file") from None
        key_id = os.environ.get("APCA_API_KEY_ID", file_values.get("APCA_API_KEY_ID"))
        secret_key = os.environ.get("APCA_API_SECRET_KEY", file_values.get("APCA_API_SECRET_KEY"))
        key_id, secret_key = _credentials(key_id, secret_key)
        if key_id is None:
            raise MarketDataError("configuration", "Alpaca market-data credentials are not configured locally")
        return cls(key_id, secret_key, min_request_interval_seconds=min_request_interval_seconds)

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            raise MarketDataError("transport", "Could not close the market-data connection") from None

    def __enter__(self) -> MarketDataClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _get_page(self, path: str, params: dict[str, object]) -> dict[str, Any]:
        if not isinstance(path, str) or path not in _PATHS:
            raise MarketDataError("validation", "Requested market-data path is not allowlisted")
        headers = {"Accept": "application/json", "User-Agent": "quantpaper-readonly-marketdata/1"}
        if self._key_id is not None:
            headers["APCA-API-KEY-ID"] = self._key_id
            headers["APCA-API-SECRET-KEY"] = self._secret_key
        try:
            # Pace every page, not merely each fetch segment. The lock also
            # serializes the shared requests.Session and prevents burst starts
            # if callers later use the same client from multiple threads.
            # This is client-local pacing, not an account-wide rate guarantee.
            with self._request_lock:
                if self._min_request_interval_seconds:
                    if self._next_request_at is not None:
                        delay = self._next_request_at - time.monotonic()
                        if delay > 0:
                            time.sleep(delay)
                    self._next_request_at = time.monotonic() + self._min_request_interval_seconds
                self._request_count += 1
                response = self._session.get(
                    _DATA_ORIGIN + path, params=params, headers=headers,
                    timeout=_TIMEOUT, allow_redirects=False,
                )
        except Exception:
            raise MarketDataError("transport", "Alpaca market-data request failed") from None
        status = getattr(response, "status_code", None)
        if type(status) is not int:
            raise MarketDataError("malformed_response", "Market-data response has no valid HTTP status")
        if 300 <= status < 400 or getattr(response, "history", []):
            raise MarketDataError("redirect", "Market-data redirects are disabled", http_status=status)
        if status != 200:
            category, message = {
                401: ("authentication", "Alpaca rejected the market-data credentials"),
                403: ("permission", "The requested market-data feed is not permitted for this account"),
                429: ("rate_limit", "Market-data rate limit reached; no automatic retry was made"),
            }.get(status, ("http", "Alpaca returned an unsuccessful market-data response"))
            raise MarketDataError(category, message, http_status=status)
        response_url = getattr(response, "url", None)
        if response_url is not None:
            try:
                parsed = urlsplit(response_url)
                valid_url = (parsed.scheme == "https" and parsed.netloc == "data.alpaca.markets"
                             and parsed.path == path and not parsed.fragment)
            except Exception:
                valid_url = False
            if not valid_url:
                raise MarketDataError("redirect", "Market-data response came from an unexpected route")
        try:
            payload = response.json()
        except Exception:
            raise MarketDataError("malformed_response", "Market-data response is not valid JSON") from None
        if not isinstance(payload, dict):
            raise MarketDataError("malformed_response", "Market-data response must be a JSON object")
        return payload

    @staticmethod
    def _records(payload: dict[str, Any], kind: str, symbol: str) -> list[dict[str, Any]]:
        if "symbol" in payload and payload["symbol"] != symbol:
            raise MarketDataError("malformed_response", "Market-data response declares a different symbol")
        mapping = payload.get(kind)
        if not isinstance(mapping, dict):
            raise MarketDataError("malformed_response", "Market-data response lacks the requested symbol mapping")
        if set(mapping) - {symbol}:
            raise MarketDataError("malformed_response", "Market-data response contains an unexpected symbol")
        raw = mapping.get(symbol, [])
        if not isinstance(raw, list) or len(raw) > _PAGE_LIMIT:
            raise MarketDataError("malformed_response", "Market-data records must be a bounded list")
        fields = ("o", "h", "l", "c", "v") if kind == "bars" else ("bp", "ap", "bs", "as")
        for record in raw:
            if not isinstance(record, dict):
                raise MarketDataError("malformed_response", "Each market-data record must be an object")
            for symbol_field in ("symbol", "S"):
                if symbol_field in record and record[symbol_field] != symbol:
                    raise MarketDataError("malformed_response", "A market-data record declares a different symbol")
            try:
                timestamp = datetime.fromisoformat(record["t"].replace("Z", "+00:00"))
                if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    raise ValueError("timestamp lacks timezone")
                if any(type(record[name]) not in (int, float) or not math.isfinite(record[name])
                       for name in fields):
                    raise ValueError("missing or nonfinite field")
            except Exception:
                raise MarketDataError("malformed_response", "Market-data record has invalid timestamp or numeric fields") from None
        try:
            # Detach the raw snapshot and disallow non-JSON/NaN extra fields.
            return json.loads(json.dumps(raw, allow_nan=False))
        except Exception:
            raise MarketDataError("malformed_response", "Market-data records contain unsupported JSON values") from None

    def fetch(self, kind: str, symbol: str, start: datetime, end: datetime,
              *, feed: str = "sip", max_pages: int = 3) -> dict[str, Any]:
        """Collect a ≤1-day interval; consumers enforce [start, end) themselves.

        `complete` refers to pagination completion only, never exchange coverage
        or absence of missing observations. The hard cap is 10 pages, default 3.
        """
        if not isinstance(kind, str) or kind not in {"bars", "quotes"}:
            raise MarketDataError("validation", "Only historical bars and quotes are supported")
        if not isinstance(feed, str):
            raise MarketDataError("validation", "Market-data feed must be text")
        if not isinstance(symbol, str):
            raise MarketDataError("validation", "Market-data symbol must be text")
        clean = symbol.strip().upper()
        if clean == "BTC-USD":
            clean = "BTC/USD"
        crypto = clean == "BTC/USD"
        if not crypto and clean not in _EQUITIES:
            raise MarketDataError("validation", "Market-data symbol is outside the supported research universe")
        if crypto:
            if feed not in {"sip", "crypto_us"}:
                raise MarketDataError("validation", "US crypto requests require the crypto_us feed")
            effective_feed = "crypto_us"
            path = f"/v1beta3/crypto/us/{kind}"
        else:
            if feed not in {"sip", "iex"}:
                raise MarketDataError("validation", "Equity feed must be sip or iex")
            if self._key_id is None:
                raise MarketDataError("configuration", "Equity market data requires local Alpaca credentials")
            effective_feed = feed
            path = f"/v2/stocks/{kind}"
        if type(max_pages) is not int or not 1 <= max_pages <= _MAX_PAGES:
            raise MarketDataError("validation", "max_pages must be an integer between 1 and 10")
        start_utc, end_utc = _utc(start), _utc(end)
        if end_utc <= start_utc or end_utc - start_utc > timedelta(days=1):
            raise MarketDataError("validation", "Market-data windows must be positive and at most one day")
        if end_utc > datetime.now(timezone.utc):
            raise MarketDataError("validation", "Market-data windows must not end in the future")
        params: dict[str, object] = {
            "symbols": clean, "start": _stamp(start_utc), "end": _stamp(end_utc),
            "sort": "asc", "limit": _PAGE_LIMIT,
        }
        if not crypto:
            params["feed"] = effective_feed
        if kind == "bars":
            params["timeframe"] = "1Min"
            if not crypto:
                params["adjustment"] = "raw"
        records: list[dict[str, Any]] = []
        observed_pages: list[str] = []
        seen_tokens: set[str] = set()
        complete = False
        for _ in range(max_pages):
            payload = self._get_page(path, dict(params))
            observed_pages.append(_stamp(datetime.now(timezone.utc)))
            records.extend(self._records(payload, kind, clean))
            if "next_page_token" not in payload:
                raise MarketDataError("malformed_response", "Market-data response lacks pagination completion metadata")
            token = payload["next_page_token"]
            if token is None:
                complete = True
                break
            if (not isinstance(token, str) or not token or len(token) > 4096
                    or not token.isascii() or not token.isprintable()):
                raise MarketDataError("pagination", "Market-data pagination token is malformed")
            if token in seen_tokens:
                raise MarketDataError("pagination", "Market-data pagination repeated a token")
            seen_tokens.add(token)
            params["page_token"] = token
        observed_at = _stamp(datetime.now(timezone.utc))
        content_hash = hashlib.sha256(json.dumps(
            records, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode()).hexdigest()
        return {
            "symbol": clean, "kind": kind, "feed": effective_feed,
            "start": _stamp(start_utc), "end": _stamp(end_utc), "observed_at": observed_at,
            "records": records, "pages": len(observed_pages), "complete": complete,
            "truncation_reason": None if complete else "max_pages_reached",
            "page_observed_at": observed_pages, "content_hash": content_hash,
        }
