"""SEC EDGAR Company Facts connector with conservative availability times."""

from __future__ import annotations

from datetime import date
import json
import math
from typing import Callable
from urllib.request import Request, urlopen

from ..source_records import FundamentalRecord
from .common import FetchBatch, after_local_date, canonical_hash


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
    request = Request(url, headers=headers)
    with urlopen(request, timeout=30) as response:
        return json.load(response)


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
        if "@" not in clean_agent or len(clean_agent) < 8:
            raise ValueError("SEC_USER_AGENT must identify you and include a contact email")
        self.headers = {"User-Agent": clean_agent, "Accept": "application/json"}
        self.get_json = get_json

    def resolve_cik(self, ticker: str) -> str:
        payload = self.get_json(SEC_TICKERS, self.headers)
        if not isinstance(payload, dict):
            raise RuntimeError("SEC ticker mapping returned an unexpected payload")
        clean = ticker.strip().upper()
        for company in payload.values():
            if isinstance(company, dict) and str(company.get("ticker", "")).upper() == clean:
                return str(company["cik_str"]).zfill(10)
        raise KeyError(f"SEC ticker mapping has no entry for {clean}")

    def fetch(self, ticker: str) -> FetchBatch[FundamentalRecord]:
        clean = ticker.strip().upper()
        cik = self.resolve_cik(clean)
        url = f"{SEC_BASE}/api/xbrl/companyfacts/CIK{cik}.json"
        payload = self.get_json(url, self.headers)
        if not isinstance(payload, dict):
            raise RuntimeError("SEC Company Facts returned an unexpected payload")
        records = parse_company_facts(payload, clean)
        return FetchBatch(
            source="sec-companyfacts",
            request={"ticker": clean, "cik": cik, "concepts": list(DEFAULT_CONCEPTS)},
            records=records,
            content_hash=canonical_hash(payload),
        )
