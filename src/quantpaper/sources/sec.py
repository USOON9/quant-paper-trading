"""SEC EDGAR Company Facts connector with conservative availability times."""

from __future__ import annotations

from datetime import date
import math
import re
from typing import Callable

from ..source_records import FundamentalRecord
from .common import FetchBatch, after_local_date, canonical_hash
from .http import get_json as bounded_get_json


SEC_BASE = "https://data.sec.gov"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
DEFAULT_CONCEPTS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "NetIncomeLoss",
    "Assets",
    "Liabilities",
    "StockholdersEquity",
    "EarningsPerShareDiluted",
    "NetCashProvidedByUsedInOperatingActivities",
)
JsonGetter = Callable[[str, dict[str, str]], object]


def _default_get_json(url: str, headers: dict[str, str]) -> object:
    return bounded_get_json(url, headers, allowed_hosts=frozenset({"data.sec.gov", "www.sec.gov"}),
                            max_bytes=25_000_000)


def _ticker(ticker: str) -> str:
    clean = ticker.strip().upper() if isinstance(ticker, str) else ""
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,15}", clean, re.ASCII):
        raise ValueError("SEC ticker is invalid")
    return clean


def _cik(value: object) -> str:
    if type(value) not in (str, int):
        raise ValueError("SEC company identifier is invalid")
    text = str(value)
    if not re.fullmatch(r"[0-9]{1,10}", text, re.ASCII) or int(text) == 0:
        raise ValueError("SEC company identifier is invalid")
    return text.zfill(10)


def _available_after_filing(filed: str) -> str:
    return after_local_date(filed, "America/New_York")


def parse_company_facts(
    payload: dict[str, object], ticker: str, concepts: tuple[str, ...] = DEFAULT_CONCEPTS
) -> list[FundamentalRecord]:
    facts = payload.get("facts", {})
    us_gaap = facts.get("us-gaap", {}) if isinstance(facts, dict) else {}
    records: dict[str, FundamentalRecord] = {}
    for concept in concepts:
        concept_payload = us_gaap.get(concept, {}) if isinstance(us_gaap, dict) else {}
        units = concept_payload.get("units", {}) if isinstance(concept_payload, dict) else {}
        if not isinstance(units, dict):
            continue
        for unit, observations in units.items():
            if not isinstance(observations, list):
                continue
            for observation in observations:
                if not isinstance(observation, dict):
                    continue
                form = str(observation.get("form", ""))
                filed = str(observation.get("filed", ""))
                period_end = str(observation.get("end", ""))
                accession = str(observation.get("accn", ""))
                if form.removesuffix("/A") not in {"10-K", "10-Q", "20-F", "40-F"}:
                    continue
                if not filed or not period_end or not accession or "val" not in observation:
                    continue
                value = observation.get("val")
                try:
                    if isinstance(value, bool):
                        raise ValueError("boolean SEC fact")
                    numeric = float(value) if value is not None else None
                    if numeric is not None and not math.isfinite(numeric):
                        raise ValueError("nonfinite SEC fact")
                    end_date = date.fromisoformat(period_end)
                    start = observation.get("start")
                    period_start = str(start) if start is not None else None
                    if period_start is not None and date.fromisoformat(period_start) > end_date:
                        raise ValueError("SEC period starts after it ends")
                    if date.fromisoformat(filed) < end_date:
                        raise ValueError("SEC fact ends after filing")
                except (TypeError, ValueError):
                    raise ValueError(f"Invalid SEC fact for {concept}") from None
                available_at = _available_after_filing(filed)
                record = FundamentalRecord(
                    instrument_id=f"US:{ticker.upper()}",
                    metric=concept,
                    period_end=period_end,
                    value=numeric,
                    unit=str(unit),
                    form=form,
                    accession_number=accession,
                    # Only a filing date is supplied; this is its conservative
                    # upper bound, not a fabricated exact acceptance timestamp.
                    filed_at=available_at,
                    available_at=available_at,
                    period_start=period_start,
                    fiscal_year=observation.get("fy"),
                    fiscal_period=observation.get("fp"),
                    frame=observation.get("frame"),
                )
                prior = records.get(record.record_id)
                if prior is not None and prior.value != record.value:
                    raise ValueError(f"Conflicting SEC values for {concept} in accession {accession}")
                # Repeated frame metadata does not turn one economic fact into two.
                records.setdefault(record.record_id, record)
    return sorted(records.values(), key=lambda row: (row.available_at, row.metric, row.period_end))


class SECCompanyFactsClient:
    def __init__(self, user_agent: str, get_json: JsonGetter = _default_get_json) -> None:
        clean_agent = user_agent.strip()
        if ("@" not in clean_agent or not 8 <= len(clean_agent) <= 256
                or any(ord(char) < 32 or ord(char) == 127 for char in clean_agent)):
            raise ValueError("SEC_USER_AGENT must identify you and include a contact email")
        self.headers = {"User-Agent": clean_agent, "Accept": "application/json"}
        self.get_json = get_json

    def resolve_cik(self, ticker: str) -> str:
        clean = _ticker(ticker)
        payload = self._read_json(SEC_TICKERS)
        if not isinstance(payload, dict):
            raise RuntimeError("SEC ticker mapping returned an unexpected payload")
        matches = [company for company in payload.values()
                   if isinstance(company, dict) and str(company.get("ticker", "")).upper() == clean]
        if not matches:
            raise KeyError("SEC ticker mapping has no matching entry")
        if len(matches) != 1:
            raise ValueError("SEC ticker mapping is ambiguous")
        return _cik(matches[0].get("cik_str"))

    def _read_json(self, url: str) -> object:
        try:
            return self.get_json(url, self.headers)
        except Exception:
            raise RuntimeError("SEC request failed") from None

    def fetch(self, ticker: str) -> FetchBatch[FundamentalRecord]:
        clean = _ticker(ticker)
        cik = self.resolve_cik(clean)
        url = f"{SEC_BASE}/api/xbrl/companyfacts/CIK{cik}.json"
        payload = self._read_json(url)
        if not isinstance(payload, dict):
            raise RuntimeError("SEC Company Facts returned an unexpected payload")
        if _cik(payload.get("cik")) != cik:
            raise ValueError("SEC Company Facts company identifier does not match the request")
        records = parse_company_facts(payload, clean)
        return FetchBatch(
            source="sec-companyfacts",
            request={"ticker": clean, "cik": cik, "concepts": list(DEFAULT_CONCEPTS)},
            records=records,
            content_hash=canonical_hash(payload),
        )
