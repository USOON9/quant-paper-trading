"""FRED/ALFRED connector retaining each observation's vintage date."""

from __future__ import annotations

from datetime import date
import math
import re
from typing import Callable
from urllib.parse import urlencode

from ..source_records import MacroRecord
from .common import FetchBatch, after_local_date, canonical_hash
from .http import get_json as bounded_get_json


FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
MAX_PAGES = 10
MAX_RECORDS = 200_000
PAGE_LIMIT = 100_000
JsonGetter = Callable[[str], object]


def _default_get_json(url: str) -> object:
    return bounded_get_json(url, {"User-Agent": "quant-paper-research/0.1"},
                            allowed_hosts=frozenset({"api.stlouisfed.org"}), max_bytes=10_000_000)


def _metadata_integer(value: object) -> int:
    if type(value) is int:
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}", value, re.ASCII):
        parsed = int(value)
    else:
        raise ValueError("FRED pagination metadata must contain nonnegative integers")
    if parsed < 0:
        raise ValueError("FRED pagination metadata must contain nonnegative integers")
    return parsed


def _request_date(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value, re.ASCII):
        raise ValueError("FRED request date must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("FRED request date is invalid") from None


def parse_fred_observations(payload: dict[str, object], series_id: str) -> list[MacroRecord]:
    if _metadata_integer(payload.get("output_type", 1)) != 1:
        raise ValueError("FRED parser requires observations by real-time period (output_type=1)")
    observations = payload.get("observations", [])
    if not isinstance(observations, list):
        raise RuntimeError("FRED observations payload is malformed")
    records: dict[tuple[str, str], MacroRecord] = {}
    for observation in observations:
        if not isinstance(observation, dict):
            raise ValueError("FRED observation must be an object")
        observation_date = str(observation.get("date", ""))
        vintage = str(observation.get("realtime_start", ""))
        raw_value = observation.get("value")
        if not observation_date or not vintage or "value" not in observation:
            raise ValueError("FRED observation lacks date, real-time start, or value")
        try:
            if isinstance(raw_value, bool):
                raise ValueError("boolean FRED value")
            value = None if raw_value in (None, ".") else float(raw_value)
            if value is not None and not math.isfinite(value):
                raise ValueError("nonfinite value")
            date.fromisoformat(observation_date)
            vintage_day = date.fromisoformat(vintage)
            realtime_end = str(observation.get("realtime_end", "9999-12-31"))
            if date.fromisoformat(realtime_end) < vintage_day:
                raise ValueError("invalid real-time interval")
        except (TypeError, ValueError):
            raise ValueError("Invalid FRED date, value, or real-time interval") from None
        record = MacroRecord(
            series_id=series_id.upper(),
            observation_date=observation_date,
            value=value,
            vintage_date=vintage,
            available_at=after_local_date(vintage, "America/Chicago"),
            realtime_end=realtime_end,
        )
        key = (observation_date, vintage)
        if key in records and records[key] != record:
            raise ValueError("Conflicting FRED values for one observation and vintage")
        records[key] = record
    return sorted(records.values(), key=lambda row: (row.vintage_date, row.observation_date))


class FREDVintageClient:
    def __init__(self, api_key: str, get_json: JsonGetter = _default_get_json) -> None:
        if len(api_key.strip()) < 16:
            raise ValueError("FRED_API_KEY is missing or invalid")
        self.api_key = api_key.strip()
        self.get_json = get_json

    def fetch(self, series_id: str, *, observation_start: str | None = None,
              realtime_start: str | None = None,
              realtime_end: str | None = None) -> FetchBatch[MacroRecord]:
        """Fetch complete results within explicit observation and vintage windows.

        An explicit end also bounds observation dates. A restricted real-time
        start may clip an initial vintage to that boundary at the provider; the
        recorded request preserves this limitation, rather than claiming a full
        vintage history. No automatic widening or fallback is performed.
        """
        clean = series_id.strip().upper()
        if not re.fullmatch(r"[A-Z0-9_]{1,64}", clean, re.ASCII):
            raise ValueError("Invalid FRED series ID")
        params = {
            "series_id": clean,
            "api_key": self.api_key,
            "file_type": "json",
            "realtime_start": "1776-07-04",
            "realtime_end": "9999-12-31",
            # Type 1 returns value + realtime_start/end fields. Type 2 is a
            # vintage-date representation and is not this parser's contract.
            "output_type": 1,
            "limit": PAGE_LIMIT,
        }
        if observation_start is not None:
            params["observation_start"] = _request_date(observation_start)
        if realtime_start is not None:
            params["realtime_start"] = _request_date(realtime_start)
            if params["realtime_start"] < "1776-07-04":
                raise ValueError("FRED real-time start precedes the supported minimum")
        if realtime_end is not None:
            params["realtime_end"] = _request_date(realtime_end)
            params["observation_end"] = params["realtime_end"]
        if params["realtime_end"] < params["realtime_start"]:
            raise ValueError("FRED real-time end precedes the requested start")
        if observation_start is not None and params["observation_start"] > params["realtime_end"]:
            raise ValueError("FRED observation start follows the requested end")
        observations: list[object] = []
        offset = 0
        expected_count: int | None = None
        page_hashes: set[str] = set()
        for _ in range(MAX_PAGES):
            page_params = {**params, "offset": offset}
            try:
                payload = self.get_json(f"{FRED_URL}?{urlencode(page_params)}")
            except Exception:
                # HTTP errors can embed a complete URL containing the API key.
                raise RuntimeError(f"FRED request failed for {clean} at offset {offset}") from None
            if not isinstance(payload, dict):
                raise RuntimeError("FRED returned an unexpected payload")
            if "error_code" in payload:
                raise RuntimeError("FRED returned an API error; check series and local credentials")
            if _metadata_integer(payload.get("output_type", 1)) != 1:
                raise RuntimeError("FRED response output type differs from the requested format")
            page = payload.get("observations", [])
            if not isinstance(page, list):
                raise RuntimeError("FRED observations payload is malformed")
            if "count" not in payload:
                raise RuntimeError("FRED response is missing pagination count")
            count = _metadata_integer(payload["count"])
            if count > MAX_RECORDS or len(page) > PAGE_LIMIT or offset + len(page) > MAX_RECORDS:
                raise RuntimeError("FRED response exceeds the bounded observation budget")
            if _metadata_integer(payload.get("offset", offset)) != offset:
                raise RuntimeError("FRED returned inconsistent pagination metadata")
            if expected_count is not None and count != expected_count:
                raise RuntimeError("FRED result changed during pagination; retry the complete fetch")
            expected_count = count
            if not page and offset < count:
                raise RuntimeError("FRED pagination ended before all observations were returned")
            page_hash = canonical_hash(page)
            if page and page_hash in page_hashes:
                raise RuntimeError("FRED repeated a page; refusing an incomplete download")
            page_hashes.add(page_hash)
            observations.extend(page)
            offset += len(page)
            if offset > count:
                raise RuntimeError("FRED returned more observations than its pagination count")
            if offset == count:
                break
        else:
            raise RuntimeError("FRED pagination exceeds the bounded page budget; no partial batch accepted")
        combined_payload = {"observations": observations}
        records = parse_fred_observations(combined_payload, clean)
        parsed_count = len(records)
        for record in records:
            if realtime_start is not None and record.vintage_date < params["realtime_start"]:
                raise ValueError("FRED vintage precedes the requested real-time start")
            if realtime_end is not None and max(record.observation_date, record.vintage_date) > params["realtime_end"]:
                raise ValueError("FRED observation or vintage follows the requested end")
        # Providers may include an earlier period-boundary observation. Validate
        # every row and its vintage bounds first, then apply the explicit date
        # filter without inferring frequency or backdating availability.
        if observation_start is not None:
            records = [record for record in records
                       if record.observation_date >= params["observation_start"]]
        safe_request = {key: value for key, value in params.items() if key != "api_key"}
        safe_request.update({
            "provider_record_count": len(observations),
            "parsed_record_count": parsed_count,
            "excluded_before_observation_start": parsed_count - len(records),
        })
        return FetchBatch(
            source="fred-alfred",
            request=safe_request,
            records=records,
            # Hash the full validated provider input, including excluded rows.
            content_hash=canonical_hash(combined_payload),
        )
