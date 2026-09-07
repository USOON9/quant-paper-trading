"""Deterministic, non-executable research evidence; no model or provider calls."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re


SCHEMA_VERSION = 1
DEFAULT_SERIES = ("DFF", "DGS10", "CPIAUCSL", "UNRATE")
MODES = ("local_observed", "reconstructed")
LIMITATIONS = (
    "Research evidence only: no signal, forecast, order, fill, or model approval is produced.",
    "Availability is strictly before as_of; a packet generated later is not a historical forward prediction.",
    "Local-observed mode also requires local ingestion before as_of; reconstructed mode does not.",
    "Yahoo adjusted daily snapshots are available only after observation, not retroactively at bar close.",
    "Daily bars are not executable quotes; age is descriptive and is not a freshness or market-session gate.",
    "SEC and FRED use conservative date-level release conventions, not verified intraday release times.",
    "Explicit cross-dataset identifiers are caller assertions, not a verified historical security master.",
    "Macro units/frequency are not stored in warehouse v4; do not infer units or combine series numerically.",
    "News contains first-seen metadata and hashes, not article text, sentiment, or complete source coverage.",
    "Missing or truncated sections do not establish that no event occurred; no imputation is performed.",
    "Hashes detect ordinary changes, not malicious rewriting without an external trusted anchor.",
)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def utc_timestamp(value: str | datetime) -> str:
    """Reject implicit zones and unsupported sub-microsecond precision."""
    if isinstance(value, str):
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})",
            value,
        ):
            raise ValueError("timestamp must be ISO 8601 with an explicit zone and <=6 fractional digits")
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must have an explicit timezone")
    try:
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (OverflowError, ValueError):
        raise ValueError("timestamp is outside the supported UTC range") from None


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9:^._/=-]{1,96}", value):
        raise ValueError(f"{label} must be an explicit identifier without whitespace or control characters")
    return value


@dataclass(frozen=True, slots=True)
class EvidenceRequest:
    instrument_id: str
    as_of: str
    fundamental_instrument_id: str | None = None
    news_entity: str | None = None
    macro_series: tuple[str, ...] = DEFAULT_SERIES
    availability_mode: str = "local_observed"
    max_records: int = 100

    def __post_init__(self) -> None:
        _identifier(self.instrument_id, "instrument_id")
        for label in ("fundamental_instrument_id", "news_entity"):
            value = getattr(self, label)
            if value is not None:
                _identifier(value, label)
        object.__setattr__(self, "as_of", utc_timestamp(self.as_of))
        if self.availability_mode not in MODES:
            raise ValueError("unsupported availability mode")
        if type(self.max_records) is not int or not 1 <= self.max_records <= 500:
            raise ValueError("max_records must be an integer between 1 and 500")
        if not isinstance(self.macro_series, (tuple, list)) or len(self.macro_series) > 20:
            raise ValueError("macro_series must contain at most 20 explicit series identifiers")
        if any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", item)
               for item in self.macro_series):
            raise ValueError("macro series IDs must contain only letters, digits, or underscores")
        series = tuple(sorted({item.upper() for item in self.macro_series}))
        object.__setattr__(self, "macro_series", series)


def _finite(value: object, field: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if type(value) not in (float, int) or not math.isfinite(value):
        raise ValueError(f"invalid numeric evidence field: {field}")


def _validate_row(kind: str, row: dict, request: EvidenceRequest) -> None:
    """Defense in depth even when a reader/fixture is replaced."""
    cutoff = request.as_of
    for name in ("available_at", "ingested_at"):
        timestamp = utc_timestamp(row[name])
        if name == "available_at" or request.availability_mode == "local_observed":
            if timestamp >= cutoff:
                raise ValueError("evidence is not strictly available before as_of")
    if not isinstance(row.get("source"), str) or not row["source"]:
        raise ValueError("evidence source is missing")
    if kind == "identity":
        if row["instrument_id"] != request.instrument_id:
            raise ValueError("identity evidence instrument mismatch")
        if utc_timestamp(row["valid_from"]) > cutoff or (row["valid_to"] is not None and utc_timestamp(row["valid_to"]) <= cutoff):
            raise ValueError("identity is not effective at the cutoff")
    elif kind == "market":
        if row["instrument_id"] != request.instrument_id:
            raise ValueError("market evidence instrument mismatch")
        if row["source"] != "yahoo" or row["interval"] != "1d" or not row["data_version"].startswith("snapshot-v2:"):
            raise ValueError("market evidence must be an observed daily Yahoo snapshot")
        bar_end = datetime.fromisoformat(utc_timestamp(row["event_time"])) + timedelta(days=1)
        if bar_end > datetime.fromisoformat(cutoff) or bar_end > datetime.fromisoformat(utc_timestamp(row["available_at"])):
            raise ValueError("market evidence contains an incomplete bar or premature availability")
        for name in ("open", "high", "low", "close", "volume"):
            _finite(row[name], name)
        if min(row[name] for name in ("open", "high", "low", "close")) <= 0 or row["volume"] < 0:
            raise ValueError("market evidence contains invalid prices or volume")
        if row["high"] < max(row["open"], row["close"], row["low"]) or row["low"] > min(
            row["open"], row["close"], row["high"]
        ):
            raise ValueError("market evidence violates OHLC bounds")
    elif kind in ("fundamentals", "macro"):
        _finite(row["value"], "value", nullable=True)
        if kind == "fundamentals" and row["instrument_id"] != request.fundamental_instrument_id:
            raise ValueError("fundamental evidence instrument mismatch")
        if kind == "fundamentals":
            if utc_timestamp(row["filed_at"]) > utc_timestamp(row["available_at"]):
                raise ValueError("fundamental availability precedes filing")
            end = date.fromisoformat(row["period_end"])
            if end > date.fromisoformat(cutoff[:10]) or (row["period_start"] is not None and date.fromisoformat(row["period_start"]) > end):
                raise ValueError("fundamental reporting period is invalid")
        else:
            if row["series_id"] not in request.macro_series:
                raise ValueError("macro evidence series was not requested")
            if any(date.fromisoformat(row[field]) > date.fromisoformat(cutoff[:10]) for field in ("observation_date", "vintage_date")):
                raise ValueError("macro evidence has a future observation or vintage")
            if date.fromisoformat(row["vintage_date"]) > date.fromisoformat(utc_timestamp(row["available_at"])[:10]):
                raise ValueError("macro availability precedes vintage")
    elif kind == "news":
        if not isinstance(row["entity_ids"], list) or any(not isinstance(x, str) for x in row["entity_ids"]):
            raise ValueError("news entities must contain a string array")
        if not isinstance(row["quality_flags"], dict):
            raise ValueError("news quality flags must contain an object")
        canonical_bytes(row["quality_flags"])
        if request.news_entity not in row["entity_ids"]:
            raise ValueError("news evidence entity mismatch")
        if not re.fullmatch(r"[a-fA-F0-9]{64}", row["title_hash"]):
            raise ValueError("news title hash is invalid")
        if max(utc_timestamp(row["published_at"]), utc_timestamp(row["first_seen_at"])) > utc_timestamp(row["available_at"]):
            raise ValueError("news availability precedes publication or first observation")


def _section(kind: str, rows: list[dict], request: EvidenceRequest, *, requested: bool = True,
             archived_truncated: bool = False) -> dict:
    warnings = []
    if not requested:
        return {"status": "NOT_REQUESTED", "count": 0, "truncated": False,
                "latest_available_at": None, "availability_age_seconds": None,
                "warnings": ["No explicit identifier was provided."], "records": []}
    # Validate overflow too: truncation must not disguise corrupt input.
    for row in rows:
        _validate_row(kind, row, request)
    truncated = len(rows) > request.max_records or archived_truncated
    selected = rows[:request.max_records]
    if truncated:
        warnings.append("Record limit reached; this is a bounded sample, not complete coverage.")
    if not selected:
        warnings.append("No eligible rows are present in this warehouse snapshot for this request.")
    if any(row.get("value", 0) is None for row in selected):
        warnings.append("Null values are retained as missing; no imputation was performed.")
    if kind == "news" and selected:
        warnings.append("Metadata only; headline text, body, and sentiment are unavailable.")
    if kind == "identity" and len(rows) > 1:
        warnings.append("Multiple eligible identity rows; no automatic identity resolution was performed.")
    records = [{"evidence_id": digest({"kind": kind, "record": row}), **row} for row in selected]
    latest = max((utc_timestamp(row["available_at"]) for row in selected), default=None)
    age = ((datetime.fromisoformat(request.as_of) - datetime.fromisoformat(latest)).total_seconds()
           if latest else None)
    return {"status": "MISSING" if not selected else "PARTIAL" if warnings else "AVAILABLE",
            "count": len(records), "truncated": truncated, "latest_available_at": latest,
            "availability_age_seconds": age, "warnings": warnings, "records": records}


def build_evidence_packet(database: Path, request: EvidenceRequest,
                          *, generated_at: datetime | None = None) -> dict:
    from .evidence_store import read_evidence_snapshot

    generated = utc_timestamp(generated_at or datetime.now(timezone.utc))
    if request.as_of > generated:
        raise ValueError("as_of cannot be later than packet generation")
    snapshot = read_evidence_snapshot(database, **asdict(request))
    sections = {
        "identity": _section("identity", snapshot["identity"], request),
        "market": _section("market", snapshot["market"], request),
        "fundamentals": _section("fundamentals", snapshot["fundamentals"], request,
                                 requested=request.fundamental_instrument_id is not None),
        "macro": {series: _section("macro", snapshot["macro"][series], request)
                  for series in request.macro_series},
        "news": _section("news", snapshot["news"], request, requested=request.news_entity is not None),
    }
    for series, section in sections["macro"].items():
        if any(row["series_id"] != series for row in section["records"]):
            raise ValueError("macro evidence is assigned to the wrong section")
    packet = {
        "schema_version": SCHEMA_VERSION, "warehouse_schema_version": 4,
        "generated_at": generated, "request": json.loads(canonical_bytes(asdict(request))),
        "research_only": True, "execution_enabled": False, "model_approval": False,
        "forward_prediction": False, "llm_called": False,
        "cutoff_policy": "strictly_before_as_of", "sections": sections,
        "limitations": list(LIMITATIONS),
    }
    packet["snapshot_hash"] = digest({"request": packet["request"], "sections": sections})
    packet["packet_hash"] = digest(packet)
    return packet


def validate_packet(packet: dict) -> dict:
    """Validate an archived packet independently of today's database or code."""
    if not isinstance(packet, dict) or type(packet.get("schema_version")) is not int or packet.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported evidence packet schema")
    unsigned = {key: value for key, value in packet.items() if key != "packet_hash"}
    if packet.get("packet_hash") != digest(unsigned):
        raise ValueError("evidence packet hash mismatch")
    for field, expected in (("research_only", True), ("execution_enabled", False),
                            ("model_approval", False), ("forward_prediction", False), ("llm_called", False)):
        if packet.get(field) is not expected:
            raise ValueError("evidence packet safety flags are invalid")
    request = EvidenceRequest(**packet["request"])
    if canonical_bytes(asdict(request)) != canonical_bytes(packet["request"]):
        raise ValueError("evidence request is not canonical")
    if type(packet.get("warehouse_schema_version")) is not int or packet.get("warehouse_schema_version") != 4 or packet.get("cutoff_policy") != "strictly_before_as_of":
        raise ValueError("unsupported warehouse or cutoff policy")
    if packet.get("limitations") != list(LIMITATIONS):
        raise ValueError("evidence limitations were changed")
    if request.as_of > utc_timestamp(packet["generated_at"]):
        raise ValueError("evidence packet has a future cutoff")
    sections = packet["sections"]
    if not isinstance(sections, dict) or not isinstance(sections.get("macro"), dict):
        raise ValueError("evidence sections must be objects")
    if packet.get("snapshot_hash") != digest({"request": packet["request"], "sections": sections}):
        raise ValueError("evidence snapshot hash mismatch")
    if set(sections) != {"identity", "market", "fundamentals", "macro", "news"} or set(sections["macro"]) != set(request.macro_series):
        raise ValueError("evidence packet section list is invalid")
    for name, section in section_items(packet):
        kind = "macro" if name.startswith("macro:") else name
        if not isinstance(section, dict) or not isinstance(section.get("records"), list):
            raise ValueError("evidence section must be an object with a record list")
        if type(section["count"]) is not int or section["count"] != len(section["records"]) or section["count"] > request.max_records:
            raise ValueError("evidence packet record count mismatch")
        if type(section["truncated"]) is not bool or (section["truncated"] and section["count"] != request.max_records):
            raise ValueError("evidence packet truncation metadata is invalid")
        rows = []
        for item in section["records"]:
            if not isinstance(item, dict):
                raise ValueError("evidence record must be an object")
            row = {key: value for key, value in item.items() if key != "evidence_id"}
            if item.get("evidence_id") != digest({"kind": kind, "record": row}):
                raise ValueError("evidence record hash mismatch")
            _validate_row(kind, row, request)
            if kind == "macro" and row["series_id"] != name.removeprefix("macro:"):
                raise ValueError("macro evidence is assigned to the wrong section")
            rows.append(row)
        if len({item["evidence_id"] for item in section["records"]}) != len(rows):
            raise ValueError("duplicate evidence records")
        requested = not ((kind == "fundamentals" and request.fundamental_instrument_id is None) or
                         (kind == "news" and request.news_entity is None))
        expected_section = _section(kind, rows, request, requested=requested,
                                    archived_truncated=section["truncated"])
        # Truncated identity may include unseen competing rows even with a limit of one.
        if kind == "identity" and section["truncated"] and len(rows) == 1:
            expected_section["status"] = "PARTIAL"
            expected_section["warnings"].append("Multiple eligible identity rows; no automatic identity resolution was performed.")
        if section != expected_section:
            raise ValueError("evidence section metadata is inconsistent with its records")
    return packet


def section_items(packet: dict) -> list[tuple[str, dict]]:
    sections = packet["sections"]
    return [("identity", sections["identity"]), ("market", sections["market"]),
            ("fundamentals", sections["fundamentals"]),
            *[(f"macro:{series}", section) for series, section in sections["macro"].items()],
            ("news", sections["news"])]


def packet_summary(packet: dict) -> dict:
    return {"packet_hash": packet["packet_hash"], "snapshot_hash": packet["snapshot_hash"],
            "as_of": packet["request"]["as_of"],
            "availability_mode": packet["request"]["availability_mode"],
            "research_only": True, "execution_enabled": False,
            "sections": {name: {key: section[key] for key in ("status", "count", "truncated")}
                         for name, section in section_items(packet)}}


def _cell(value: object) -> str:
    if value is None:
        return "Unavailable"
    # Data is untrusted text. Keep it inside inert table cells, never raw markup.
    text = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"[\x00-\x1f\x7f]", " ", text).replace("|", "&#124;").replace("`", "&#96;").replace("[", "&#91;")


def render_markdown(packet: dict) -> str:
    validate_packet(packet)
    request = packet["request"]
    lines = ["# Research Evidence Packet", "", "Research only. No forecast, trading signal, or order was generated.", "",
             f"- Instrument: `{request['instrument_id']}`",
             f"- Explicit fundamentals ID: `{request['fundamental_instrument_id']}`",
             f"- Explicit news entity: `{request['news_entity']}`",
             f"- Cutoff (exclusive): `{request['as_of']}`",
             f"- Availability mode: `{request['availability_mode']}`",
             f"- Generated at: `{packet['generated_at']}`",
             f"- Packet SHA-256: `{packet['packet_hash']}`", "", "## Coverage", "",
             "AVAILABLE means eligible rows exist, not that the dataset is complete, current, or trade-ready.", "",
             "| Section | Status | Records | Truncated | Latest availability |",
             "| --- | --- | ---: | --- | --- |"]
    for name, section in section_items(packet):
        lines.append("| " + " | ".join(_cell(x) for x in (name, section["status"], section["count"],
                    section["truncated"], section["latest_available_at"])) + " |")
    for name, section in section_items(packet):
        lines.extend(["", f"## {_cell(name)}", ""])
        for warning in section["warnings"]:
            lines.extend([f"- {_cell(warning)}"])
        if section["records"]:
            lines.extend(["", "Only the first five selected records are displayed here; packet.json retains the bounded sample.", ""])
            if name == "market":
                columns = ("event_time", "close", "volume", "source", "available_at")
            elif name == "fundamentals":
                columns = ("metric", "period_start", "period_end", "value", "unit", "available_at")
            elif name.startswith("macro:"):
                columns = ("series_id", "observation_date", "value", "vintage_date", "available_at")
            elif name == "news":
                columns = ("event_id", "published_at", "first_seen_at", "source", "title_hash")
            else:
                columns = ("instrument_id", "symbol", "asset_class", "source", "available_at")
            lines.extend(["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"])
            for record in section["records"][:5]:
                lines.append("| " + " | ".join(_cell(record.get(column)) for column in columns) + " |")
    lines.extend(["", "## Limitations", "", *[f"- {text}" for text in packet["limitations"]], ""])
    return "\n".join(lines)
