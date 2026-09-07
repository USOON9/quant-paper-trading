"""Bounded, paced JSON reads from fixed official SEC/FRED hosts only."""

from __future__ import annotations

import json
import ssl
from threading import Lock
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

import certifi


OFFICIAL_HOSTS = frozenset({"data.sec.gov", "www.sec.gov", "api.stlouisfed.org"})
MAX_RESPONSE_BYTES = 25_000_000
REQUEST_INTERVAL_SECONDS = 0.25
_request_lock = Lock()
_last_started: float | None = None


class SourceHTTPError(RuntimeError):
    """Only a fixed category is exposed, never a request URL or credential."""

    def __init__(self, category: str):
        self.category = category
        super().__init__(f"Source JSON request failed ({category})")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise SourceHTTPError("redirect")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("nonfinite JSON constant")


def _verified_tls_context():
    """Supplement system roots without weakening certificate or hostname checks."""
    try:
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=certifi.where())
        if context.verify_mode != ssl.CERT_REQUIRED or context.check_hostname is not True:
            raise ValueError("verified client context required")
        return context
    except Exception:
        raise SourceHTTPError("tls_configuration") from None


def _transport_category(error: Exception) -> str:
    """Classify exception types only, without rendering private URLs or messages."""
    pending, seen = [error], set()
    tls_error = False
    while pending and len(seen) < 16:
        item = pending.pop()
        if not isinstance(item, BaseException) or id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, ssl.SSLCertVerificationError):
            return "tls_verification"
        tls_error = tls_error or isinstance(item, ssl.SSLError)
        pending.extend((item.__cause__, item.__context__))
        if isinstance(item, URLError):
            pending.append(item.reason)
    return "tls_error" if tls_error else "transport"


def get_json(url: str, headers: dict[str, str], *, allowed_hosts: frozenset[str], max_bytes: int):
    """Read one JSON response with no redirects, retries, or ambient proxy use.

    The 30-second timeout is a transport timeout, not a whole-refresh deadline.
    Request starts share one process-local rate limit across both source clients.
    """
    global _last_started
    if (type(max_bytes) is not int or not 1 <= max_bytes <= MAX_RESPONSE_BYTES
            or not isinstance(allowed_hosts, frozenset) or not allowed_hosts
            or not allowed_hosts <= OFFICIAL_HOSTS):
        raise SourceHTTPError("invalid_request")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in allowed_hosts
                or parsed.port not in (None, 443) or parsed.username is not None
                or parsed.password is not None or parsed.fragment):
            raise SourceHTTPError("invalid_url")
        request = Request(url, headers=headers, method="GET")
        opener = build_opener(ProxyHandler({}), _NoRedirect(),
                              HTTPSHandler(context=_verified_tls_context()))
        with _request_lock:
            current = time.monotonic()
            if _last_started is not None:
                delay = REQUEST_INTERVAL_SECONDS - (current - _last_started)
                if delay > 0:
                    time.sleep(min(delay, REQUEST_INTERVAL_SECONDS))
            _last_started = time.monotonic()
            response = opener.open(request, timeout=30)
        with response:
            if not 200 <= response.status < 300:
                raise SourceHTTPError("http_status")
            if response.geturl() != url:
                raise SourceHTTPError("redirect")
            length = response.headers.get("Content-Length")
            if length is not None:
                if not length.isascii() or not length.isdecimal():
                    raise SourceHTTPError("invalid_response")
                if int(length) > max_bytes:
                    raise SourceHTTPError("response_too_large")
            payload = response.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise SourceHTTPError("response_too_large")
        return json.loads(payload, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except SourceHTTPError:
        raise
    except HTTPError:
        raise SourceHTTPError("http_status") from None
    except ssl.SSLCertVerificationError:
        # This exception also inherits ValueError; classify it before JSON errors.
        raise SourceHTTPError("tls_verification") from None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise SourceHTTPError("invalid_response") from None
    except Exception as error:
        raise SourceHTTPError(_transport_category(error)) from None
