"""Eleven deterministic observed-context features, without model or I/O work.

The frozen quality rules are research assumptions, not calibrated freshness or
exchange-calendar guarantees.  Current adjusted prices and current macro
definitions do not establish a historical forward prediction.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import math
import re
import statistics

from .evidence import digest, utc_timestamp
from ..sources.fred_metadata import validate_metadata_record


CONTRACT_ID = "observed_context_v1"
CORE_SERIES = ("DFF", "DGS10", "CPIAUCSL", "UNRATE")
FEATURE_NAMES = (
    "market.return_1_observed_bar", "market.return_5_observed_bars",
    "market.return_20_observed_bars", "market.volatility_20_observed_intervals",
    "market.range_fraction", "market.volume_to_mean_20_observed_bars",
    "macro.DFF.level", "macro.DGS10.level", "macro.CPIAUCSL.level", "macro.UNRATE.level",
    "macro.DGS10_minus_DFF.spread_pp",
)
_RESULT_FIELDS = (
    "status", "value", "unit", "reason", "source_evidence_ids",
    "metadata_record_hashes", "latest_input_available_at",
)
_DEFINITIONS = {
    "DFF": ("Percent", "Daily, 7-Day", "Not Seasonally Adjusted"),
    "DGS10": ("Percent", "Daily", "Not Seasonally Adjusted"),
    "CPIAUCSL": ("Index 1982-1984=100", "Monthly", "Seasonally Adjusted"),
    "UNRATE": ("Percent", "Monthly", "Seasonally Adjusted"),
}
_MACRO_MAX_DAYS = {"DFF": 7, "DGS10": 7, "CPIAUCSL": 62, "UNRATE": 62}
_MACRO_MAX_OBSERVATION_DAYS = {"DFF": 7, "DGS10": 7, "CPIAUCSL": 100, "UNRATE": 100}
_REASONS = {
    "AVAILABLE": None,
    "INSUFFICIENT_HISTORY": "The required number of observed market bars is unavailable.",
    "STALE_MARKET": "The latest market event exceeds the seven-calendar-day age limit.",
    "EXCESSIVE_MARKET_GAP": "An observed market date gap exceeds seven calendar days.",
    "ZERO_DENOMINATOR": "The required denominator is zero; no substitute is used.",
    "MISSING_METADATA": "No strictly eligible macro metadata record was supplied.",
    "STALE_METADATA": "Macro metadata observation age exceeds thirty days.",
    "DEFINITION_MISMATCH": "Macro units, frequency, or seasonal adjustment differs from the frozen definition.",
    "MISSING_OBSERVATION": "No eligible macro observation was supplied.",
    "MISSING_VALUE": "The latest macro observation is null; older values are not substituted.",
    "STALE_OBSERVATION": "The latest macro observation availability age exceeds the series limit.",
    "STALE_OBSERVATION_DATE": "The latest macro observation date exceeds the series calendar-age limit.",
    "IDENTITY_BLOCKED": "No eligible matching single-source equity description binds these inputs.",
    "INPUT_BLOCKED": "At least one required macro level is unavailable.",
    "OBSERVATION_DATE_MISMATCH": "The latest rate observations have different dates; no alignment is inferred.",
    "NUMERIC_ERROR": "The calculation cannot produce a finite numeric result.",
}
_MARKET_FIELDS = frozenset({
    "evidence_id", "instrument_id", "event_time", "interval", "open", "high", "low", "close",
    "volume", "available_at", "ingested_at", "source", "data_version",
})
_MACRO_FIELDS = frozenset({
    "evidence_id", "series_id", "observation_date", "value", "vintage_date", "available_at",
    "ingested_at", "source", "availability_basis",
})


def feature_contract() -> dict:
    """Return the complete hashable formula, status, and quality specification."""
    features = {}
    for name, horizon in zip(FEATURE_NAMES[:3], (1, 5, 20), strict=True):
        features[name] = {"formula": f"close[-1] / close[-{horizon + 1}] - 1",
                          "required_market_bars": horizon + 1, "unit": "fraction"}
    features[FEATURE_NAMES[3]] = {
        "formula": "sample_stdev([close[i] / close[i-1] - 1 for the latest 20 observed intervals], ddof=1)",
        "required_market_bars": 21, "unit": "fraction", "annualized": False,
    }
    features[FEATURE_NAMES[4]] = {
        "formula": "(high[-1] - low[-1]) / open[-1]",
        "required_market_bars": 1, "unit": "fraction",
    }
    features[FEATURE_NAMES[5]] = {
        "formula": "volume[-1] / arithmetic_mean(volume[-20:])",
        "required_market_bars": 20, "unit": "ratio", "mean_includes_latest_bar": True,
    }
    for series in CORE_SERIES:
        features[f"macro.{series}.level"] = {
            "formula": "value of the latest observation_date, including a null latest value",
            "series_id": series, "unit": _DEFINITIONS[series][0],
        }
    features[FEATURE_NAMES[-1]] = {
        "formula": "latest DGS10 value - latest DFF value, only for exactly equal observation_date",
        "unit": "percentage_points", "requires_available_levels": ["macro.DFF.level", "macro.DGS10.level"],
    }
    return {
        "contract_id": CONTRACT_ID, "feature_names": list(FEATURE_NAMES), "features": features,
        "result_fields": list(_RESULT_FIELDS), "status_reasons": dict(_REASONS),
        "quality_policies": {
            "availability_mode": "local_observed",
            "source_cutoff": "available_at < as_of AND ingested_at < as_of for every supplied row",
            "metadata_cutoff": "available_at = observed_at < as_of",
            "market_source": "yahoo", "market_interval": "1d", "market_version_prefix": "snapshot-v2:",
            "market_completion": "event_time + 24 hours <= available_at and <= as_of",
            "market_latest_age": "as_of UTC date minus latest event UTC date <= 7 calendar days",
            "market_window_gaps": "every consecutive observed UTC date difference <= 7 calendar days",
            "market_calendar_verified": False,
            "macro_source": "fred-alfred",
            "macro_availability_basis": "vintage_date_end_america_chicago",
            "macro_max_availability_age_days": dict(_MACRO_MAX_DAYS),
            "macro_age_basis": "as_of minus latest row available_at, inclusive maximum elapsed days",
            "macro_max_observation_date_age_days": dict(_MACRO_MAX_OBSERVATION_DAYS),
            "macro_observation_date_age_basis": "as_of UTC date minus latest observation_date, inclusive maximum calendar days",
            "macro_observation_date_age_rationale": {
                "daily_rates": "Seven calendar days bounds current rate context independently of recent revisions.",
                "monthly_series": "One hundred calendar days allows reference-month-first dating while independently bounding old revised periods.",
                "calibration": "Frozen research assumptions, not calibrated economic freshness or release-calendar guarantees.",
                "combined_gate": "Both availability-age and observation-date-age limits must pass; a new revision never resets the observation date.",
            },
            "metadata_max_observation_age_days": 30,
            "metadata_age_basis": "as_of minus metadata observed_at, inclusive maximum elapsed days",
            "macro_definitions": {series: dict(zip(
                ("units", "frequency", "seasonal_adjustment"), _DEFINITIONS[series], strict=True
            )) for series in CORE_SERIES},
            "market_gate_order": ["INSUFFICIENT_HISTORY", "STALE_MARKET", "EXCESSIVE_MARKET_GAP"],
            "macro_gate_order": ["MISSING_METADATA", "DEFINITION_MISMATCH", "STALE_METADATA",
                                 "MISSING_OBSERVATION", "MISSING_VALUE", "STALE_OBSERVATION",
                                 "STALE_OBSERVATION_DATE"],
            "spread_gate_order": ["INPUT_BLOCKED", "OBSERVATION_DATE_MISMATCH"],
            "packet_identity_mask": {
                "applied_by": "research.feature_packet.build_packet after compute_features",
                "affected_features": list(FEATURE_NAMES[:6]),
                "pass_status": "MATCHED_SOURCE_DESCRIPTION",
                "binding_gate_order": [
                    "CATALOG_UNAVAILABLE_AT_CUTOFF", "CURRENT_IDENTITY_UNRESOLVED",
                    "CATALOG_IDENTITY_UNRESOLVED", "SOURCE_DESCRIPTION_MISMATCH",
                    "UNSUPPORTED_SOURCE_OR_ASSET",
                ],
                "catalog_cutoff": "catalog.generated_at < feature decision as_of",
                "current_identity_cutoff": "local-observed resolution at feature as_of; available_at and ingested_at strictly before cutoff and effective interval contains cutoff",
                "catalog_identity_cutoff": "retain the catalog's own identity cutoff; do not reinterpret historical validity at the feature cutoff",
                "allowed_resolution_statuses": ["SINGLE_ASSERTION", "EQUIVALENT_ASSERTIONS"],
                "descriptor_match": "current and catalog resolved_attributes must be exactly equal",
                "supported_description": {"source": "yahoo", "asset_class": "equity",
                                          "instrument_id": "YF:{symbol}"},
                "blocked_fields": {"status": "IDENTITY_BLOCKED", "value": None,
                                   "reason": _REASONS["IDENTITY_BLOCKED"]},
                "preserved_fields": ["unit", "source_evidence_ids", "metadata_record_hashes",
                                     "latest_input_available_at"],
                "underlying_quality": "unmasked_market_quality retains all six original calculation statuses in the packet",
                "macro_features_masked": False,
                "hash_binding": "feature_hash binds contract_hash, input_hashes, identity_binding and final feature results; packet_hash binds the complete packet",
                "historical_identity_verified": False,
            },
            "invalid_input": "raise ValueError; never silently drop malformed or unavailable rows",
            "null_policy": "latest null remains missing; never fall back to an older value",
            "provenance": "all selected window rows, including rows checked for gaps; latest macro row only",
            "evidence_ids": "verify digest of kind and original record; sort unique IDs in each result",
            "latest_input_available_at": "maximum selected source available_at and supplied metadata observed_at",
        },
        "limitations": [
            "Research context only; no prediction, model approval, broker call, or execution permission.",
            "Age and gap thresholds are frozen research assumptions, not calibrated trading safety gates.",
            "A recent macro revision does not make an ancient observation period current.",
            "The packet identity mask verifies matching eligible descriptions, not a historical security master.",
            "Observed bars are not verified trading sessions or executable quotes.",
            "No annualization, forward filling, resampling, interpolation, or missing-value imputation.",
            "No inflation, year-over-year transformation, or unit conversion is performed.",
            "Current adjusted history and current definitions do not establish historical knowledge.",
            "Source hashes are provenance references, not externally authenticated provider signatures.",
        ],
    }


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Feature timestamps must be explicit ISO strings")
    try:
        return datetime.fromisoformat(utc_timestamp(value))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Invalid feature timestamp") from None


def _date(value: object) -> date:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value, re.ASCII) is None:
        raise ValueError("Invalid feature observation date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("Invalid feature observation date") from None


def _finite(value: object, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    try:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("Invalid feature numeric value")
    except (ValueError, OverflowError, TypeError):
        raise ValueError("Invalid feature numeric value") from None


def _row(row: object, kind: str, cutoff: datetime) -> None:
    expected = _MARKET_FIELDS if kind == "market" else _MACRO_FIELDS
    if not isinstance(row, dict) or set(row) != expected:
        raise ValueError("Invalid feature source record fields")
    evidence_id = row["evidence_id"]
    try:
        expected_id = digest({"kind": kind, "record": {k: v for k, v in row.items() if k != "evidence_id"}})
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Invalid feature evidence provenance") from None
    if (not isinstance(evidence_id, str) or re.fullmatch(r"[0-9a-f]{64}", evidence_id, re.ASCII) is None
            or evidence_id != expected_id):
        raise ValueError("Invalid feature evidence provenance")
    if _time(row["available_at"]) >= cutoff or _time(row["ingested_at"]) >= cutoff:
        raise ValueError("Feature source was not strictly known before as_of")


def _validate_inputs(market_rows, macro_rows, metadata, cutoff):
    if not isinstance(market_rows, list) or len(market_rows) > 500:
        raise ValueError("Feature market input must be a bounded list")
    if not isinstance(macro_rows, dict) or set(macro_rows) != set(CORE_SERIES):
        raise ValueError("Feature macro input must contain exactly the core series")
    if not isinstance(metadata, dict) or set(metadata) != set(CORE_SERIES):
        raise ValueError("Feature metadata must contain exactly the core series")
    seen_dates, instruments = set(), set()
    for row in market_rows:
        _row(row, "market", cutoff)
        if (not isinstance(row["instrument_id"], str)
                or re.fullmatch(r"[A-Za-z0-9:^._/=-]{1,96}", row["instrument_id"]) is None):
            raise ValueError("Invalid feature market instrument")
        instruments.add(row["instrument_id"])
        if (row["source"] != "yahoo" or row["interval"] != "1d"
                or not isinstance(row["data_version"], str) or not row["data_version"].startswith("snapshot-v2:")):
            raise ValueError("Feature market source is not an observed Yahoo daily snapshot")
        event = _time(row["event_time"])
        if event.date() in seen_dates:
            raise ValueError("Feature market UTC dates must be unique")
        seen_dates.add(event.date())
        try:
            complete_at = event + timedelta(days=1)
        except OverflowError:
            raise ValueError("Invalid feature market event date") from None
        if complete_at > min(cutoff, _time(row["available_at"])):
            raise ValueError("Feature market bar is incomplete or prematurely available")
        for field in ("open", "high", "low", "close", "volume"):
            _finite(row[field])
        if min(row[field] for field in ("open", "high", "low", "close")) <= 0 or row["volume"] < 0:
            raise ValueError("Feature market prices or volume are invalid")
        if (row["high"] < max(row["open"], row["low"], row["close"])
                or row["low"] > min(row["open"], row["high"], row["close"])):
            raise ValueError("Feature market OHLC bounds are invalid")
    if len(instruments) > 1:
        raise ValueError("Feature market input must describe one instrument")
    ordered_market = sorted(market_rows, key=lambda row: _time(row["event_time"]))
    ordered_macro = {}
    for series in CORE_SERIES:
        rows = macro_rows[series]
        if not isinstance(rows, list) or len(rows) > 500:
            raise ValueError("Feature macro input must be a bounded list")
        seen = set()
        for row in rows:
            _row(row, "macro", cutoff)
            if (row["series_id"] != series or row["source"] != "fred-alfred"
                    or row["availability_basis"] != "vintage_date_end_america_chicago"):
                raise ValueError("Feature macro source or series is invalid")
            observation, vintage = _date(row["observation_date"]), _date(row["vintage_date"])
            if observation in seen:
                raise ValueError("Feature macro observation dates must be unique")
            seen.add(observation)
            if max(observation, vintage) > cutoff.date() or vintage > _time(row["available_at"]).date():
                raise ValueError("Feature macro observation or vintage is in the future")
            _finite(row["value"], nullable=True)
        ordered_macro[series] = sorted(rows, key=lambda row: row["observation_date"])
        definition = metadata[series]
        if definition is not None:
            validate_metadata_record(definition)
            if definition["series_id"] != series:
                raise ValueError("Feature metadata series does not match its section")
            if _time(definition["available_at"]) >= cutoff:
                raise ValueError("Feature metadata was not strictly known before as_of")
    return ordered_market, ordered_macro


def _result(status, value, unit, rows=(), definitions=()):
    if status not in _REASONS:
        raise ValueError("Unsupported feature result status")
    definitions = [record for record in definitions if record is not None]
    available = [_time(row["available_at"]) for row in rows]
    available.extend(_time(record["observed_at"]) for record in definitions)
    if status == "AVAILABLE":
        _finite(value)
        value = float(value)
    else:
        value = None
    return {
        "status": status, "value": value, "unit": unit, "reason": _REASONS[status],
        "source_evidence_ids": sorted({row["evidence_id"] for row in rows}),
        "metadata_record_hashes": sorted({digest(record) for record in definitions}),
        "latest_input_available_at": utc_timestamp(max(available)) if available else None,
    }


def _market_feature(rows, count, cutoff, unit, calculate):
    selected = rows[-count:]
    status = None
    if len(selected) < count:
        status = "INSUFFICIENT_HISTORY"
    elif (cutoff.date() - _time(selected[-1]["event_time"]).date()).days > 7:
        status = "STALE_MARKET"
    elif any((_time(right["event_time"]).date() - _time(left["event_time"]).date()).days > 7
             for left, right in zip(selected, selected[1:])):
        status = "EXCESSIVE_MARKET_GAP"
    if status:
        return _result(status, None, unit, selected)
    try:
        value = calculate(selected)
        if value is None:
            return _result("ZERO_DENOMINATOR", None, unit, selected)
        _finite(value)
    except (ArithmeticError, ValueError):
        return _result("NUMERIC_ERROR", None, unit, selected)
    return _result("AVAILABLE", value, unit, selected)


def _macro_level(series, rows, definition, cutoff):
    selected = rows[-1:]
    status = None
    if definition is None:
        status = "MISSING_METADATA"
    elif tuple(definition[field] for field in ("units", "frequency", "seasonal_adjustment")) != _DEFINITIONS[series]:
        status = "DEFINITION_MISMATCH"
    elif cutoff - _time(definition["observed_at"]) > timedelta(days=30):
        status = "STALE_METADATA"
    elif not selected:
        status = "MISSING_OBSERVATION"
    elif selected[0]["value"] is None:
        status = "MISSING_VALUE"
    elif cutoff - _time(selected[0]["available_at"]) > timedelta(days=_MACRO_MAX_DAYS[series]):
        status = "STALE_OBSERVATION"
    elif (cutoff.date() - _date(selected[0]["observation_date"])).days > _MACRO_MAX_OBSERVATION_DAYS[series]:
        status = "STALE_OBSERVATION_DATE"
    return _result(status or "AVAILABLE", None if status else selected[0]["value"],
                   _DEFINITIONS[series][0], selected, [definition])


def compute_features(market_rows: list[dict], macro_rows: dict[str, list[dict]],
                     metadata: dict[str, dict | None], *, as_of: str) -> dict:
    """Compute exactly the frozen slots after validating every supplied record."""
    cutoff = _time(as_of)
    market, macro = _validate_inputs(market_rows, macro_rows, metadata, cutoff)
    result = {}
    for name, horizon in zip(FEATURE_NAMES[:3], (1, 5, 20), strict=True):
        result[name] = _market_feature(market, horizon + 1, cutoff, "fraction",
                                       lambda rows: rows[-1]["close"] / rows[0]["close"] - 1)

    def volatility(rows):
        returns = [right["close"] / left["close"] - 1 for left, right in zip(rows, rows[1:])]
        for value in returns:
            _finite(value)
        return statistics.stdev(returns)

    result[FEATURE_NAMES[3]] = _market_feature(
        market, 21, cutoff, "fraction", volatility,
    )
    result[FEATURE_NAMES[4]] = _market_feature(
        market, 1, cutoff, "fraction", lambda rows: (rows[-1]["high"] - rows[-1]["low"]) / rows[-1]["open"],
    )

    def volume_ratio(rows):
        mean = statistics.mean(row["volume"] for row in rows)
        return rows[-1]["volume"] / mean if mean != 0 else None

    result[FEATURE_NAMES[5]] = _market_feature(market, 20, cutoff, "ratio", volume_ratio)
    for series in CORE_SERIES:
        result[f"macro.{series}.level"] = _macro_level(series, macro[series], metadata[series], cutoff)
    inputs = [result[f"macro.{series}.level"] for series in ("DGS10", "DFF")]
    selected = macro["DGS10"][-1:] + macro["DFF"][-1:]
    definitions = [metadata[series] for series in ("DGS10", "DFF")]
    if any(item["status"] != "AVAILABLE" for item in inputs):
        spread = _result("INPUT_BLOCKED", None, "percentage_points", selected, definitions)
    elif selected[0]["observation_date"] != selected[1]["observation_date"]:
        spread = _result("OBSERVATION_DATE_MISMATCH", None, "percentage_points", selected, definitions)
    else:
        value = selected[0]["value"] - selected[1]["value"]
        spread = _result("AVAILABLE" if math.isfinite(value) else "NUMERIC_ERROR",
                         value, "percentage_points", selected, definitions)
    result[FEATURE_NAMES[-1]] = spread
    return result
