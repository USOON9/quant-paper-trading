"""Provider-neutral records accepted by the point-in-time warehouse."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json


@dataclass(frozen=True, slots=True)
class FundamentalRecord:
    instrument_id: str
    metric: str
    period_end: str
    value: float | None
    unit: str
    form: str
    accession_number: str
    filed_at: str
    available_at: str
    source: str = "sec-companyfacts"
    period_start: str | None = None
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    frame: str | None = None
    availability_basis: str = "filing_date_end_america_new_york"

    @property
    def record_id(self) -> str:
        # Quarter and year-to-date facts can share an end date and accession.
        identity = (self.source, self.instrument_id, self.metric, self.period_start,
                    self.period_end, self.unit, self.accession_number)
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def as_tuple(self) -> tuple[object, ...]:
        return (
            self.record_id,
            self.instrument_id,
            self.metric,
            self.period_start,
            self.period_end,
            self.value,
            self.unit,
            self.form,
            self.accession_number,
            self.filed_at,
            self.available_at,
            self.source,
            self.fiscal_year,
            self.fiscal_period,
            self.frame,
            self.availability_basis,
        )


@dataclass(frozen=True, slots=True)
class MacroRecord:
    series_id: str
    observation_date: str
    value: float | None
    vintage_date: str
    available_at: str
    source: str = "fred-alfred"
    realtime_end: str = "9999-12-31"
    availability_basis: str = "vintage_date_end_america_chicago"

    def as_tuple(self) -> tuple[object, ...]:
        return (
            self.series_id,
            self.observation_date,
            self.value,
            self.vintage_date,
            self.available_at,
            self.source,
            self.realtime_end,
            self.availability_basis,
        )


@dataclass(frozen=True, slots=True)
class NewsRecord:
    event_id: str
    published_at: str
    first_seen_at: str
    available_at: str
    source: str
    title_hash: str
    raw_uri: str | None
    entity_ids: str
    quality_flags: str

    def as_tuple(self) -> tuple[object, ...]:
        return (
            self.event_id,
            self.published_at,
            self.first_seen_at,
            self.available_at,
            self.source,
            self.title_hash,
            self.raw_uri,
            self.entity_ids,
            self.quality_flags,
        )
