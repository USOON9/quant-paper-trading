"""FRED/ALFRED connector retaining each observation's vintage date."""

from __future__ import annotations

from datetime import date
import json
import math
import re
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..source_records import MacroRecord
from .common import FetchBatch, after_local_date, canonical_hash


FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
JsonGetter = Callable[[str], object]


def _default_get_json(url: str) -> object:
    request = Request(url, headers={"User-Agent": "quant-paper-research/0.1"})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def parse_fred_observations(payload: dict[str, object], series_id: str) -> list[MacroRecord]:
    if int(payload.get("output_type", 1)) != 1:
        raise ValueError("FRED parser requires observations by real-time period (output_type=1)")
    observations = payload.get("observations", [])
    if not isinstance(observations, list):
        raise RuntimeError("FRED observations payload is malformed")
    records: dict[tuple[str, str], MacroRecord] = {}
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        observation_date = str(observation.get("date", ""))
        vintage = str(observation.get("realtime_start", ""))
        raw_value = observation.get("value")
        if not observation_date or not vintage or "value" not in observation:
            raise ValueError("FRED observation lacks date, real-time start, or value")
        try:
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

    def fetch(self, series_id: str) -> FetchBatch[MacroRecord]:
        clean = series_id.strip().upper()
        if not re.fullmatch(r"[A-Z0-9_]+", clean):
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
            "limit": 100000,
        }
        observations: list[object] = []
        offset = 0
        expected_count: int | None = None
        page_hashes: set[str] = set()
        while True:
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
            if int(payload.get("output_type", 1)) != 1:
                raise RuntimeError("FRED response output type differs from the requested format")
            page = payload.get("observations", [])
            if not isinstance(page, list):
                raise RuntimeError("FRED observations payload is malformed")
            if "count" not in payload:
                raise RuntimeError("FRED response is missing pagination count")
            count = int(payload["count"])
            if count < 0 or int(payload.get("offset", offset)) != offset:
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
        combined_payload = {"observations": observations}
        records = parse_fred_observations(combined_payload, clean)
        safe_request = {key: value for key, value in params.items() if key != "api_key"}
        return FetchBatch(
            source="fred-alfred",
            request=safe_request,
            records=records,
            content_hash=canonical_hash(combined_payload),
        )
