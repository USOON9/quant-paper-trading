"""Pure, conservative grouping of eligible provider identity assertions.

Descriptor agreement is not proof of historical security identity. Every raw
assertion is retained; this module performs no alias lookup or data mutation.
"""

from __future__ import annotations

from .evidence import MODES, _identifier, canonical_bytes, digest, utc_timestamp


SCHEMA_VERSION = 1
ROW_FIELDS = frozenset({
    "instrument_id", "symbol", "asset_class", "primary_exchange", "valid_from",
    "valid_to", "available_at", "ingested_at", "source",
})
ATTRIBUTE_FIELDS = ("source", "instrument_id", "symbol", "asset_class", "primary_exchange")
BASE_WARNINGS = (
    "Descriptor agreement is a provider assertion, not a verified historical security master.",
    "No aliases or cross-source identity equivalence are inferred; every raw assertion and its effective dates are retained.",
    "Availability does not establish that an identity resolution or a model prediction existed at the cutoff.",
)


def _row_time(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("identity assertion timestamps must be explicit ISO strings")
    return utc_timestamp(value)


def _validate_assertion(row: object, *, instrument_id: str, cutoff: str, mode: str) -> dict:
    if not isinstance(row, dict) or set(row) != ROW_FIELDS:
        raise ValueError("identity assertion must contain exactly the warehouse identity fields")
    for field in ("instrument_id", "symbol", "asset_class", "source"):
        _identifier(row[field], field)
    if row["primary_exchange"] is not None:
        _identifier(row["primary_exchange"], "primary_exchange")
    if row["instrument_id"] != instrument_id:
        raise ValueError("identity assertion does not match the requested instrument")

    available = _row_time(row["available_at"])
    ingested = _row_time(row["ingested_at"])
    valid_from = _row_time(row["valid_from"])
    valid_to = None if row["valid_to"] is None else _row_time(row["valid_to"])
    if available >= cutoff or (mode == "local_observed" and ingested >= cutoff):
        raise ValueError("identity assertion was not strictly known before the cutoff")
    if valid_to is not None and valid_to <= valid_from:
        raise ValueError("identity assertion validity interval is empty or reversed")
    if valid_from > cutoff or (valid_to is not None and cutoff >= valid_to):
        raise ValueError("identity assertion is not effective at the cutoff")
    # All fields are now strings or null, so this copy preserves raw values
    # without retaining a caller-owned mutable container.
    return dict(row)


def resolve_identity(rows: list[dict], *, instrument_id: str, as_of: str,
                     availability_mode: str, complete: bool = True) -> dict:
    """Group exact descriptors without inventing a canonical security identity.

    Inputs must already be eligible at the requested cutoff. Ineligible rows,
    duplicate raw assertions, invalid types, and unknown fields fail closed
    rather than being silently dropped. ``complete=False`` always leaves the
    result unresolved, including when the supplied subset is empty or agrees.
    """
    _identifier(instrument_id, "instrument_id")
    if not isinstance(as_of, str):
        raise ValueError("identity cutoff must be an explicit ISO string")
    cutoff = utc_timestamp(as_of)
    if not isinstance(availability_mode, str) or availability_mode not in MODES:
        raise ValueError("unsupported identity availability mode")
    if type(complete) is not bool:
        raise ValueError("identity completeness must be a boolean")
    if not isinstance(rows, list):
        raise ValueError("identity assertions must be supplied as a list")

    assertions_by_id = {}
    groups_by_id = {}
    for raw in rows:
        row = _validate_assertion(raw, instrument_id=instrument_id, cutoff=cutoff,
                                  mode=availability_mode)
        assertion_id = digest(row)
        if assertion_id in assertions_by_id:
            raise ValueError("duplicate raw identity assertion")
        assertions_by_id[assertion_id] = {"assertion_id": assertion_id, **row}
        attributes = {field: row[field] for field in ATTRIBUTE_FIELDS}
        group_id = digest(attributes)
        group = groups_by_id.setdefault(group_id, {"attributes": attributes, "assertion_ids": []})
        group["assertion_ids"].append(assertion_id)

    assertions = [assertions_by_id[key] for key in sorted(assertions_by_id)]
    groups = []
    for key in sorted(groups_by_id):
        group = groups_by_id[key]
        groups.append({"attributes": group["attributes"],
                       "assertion_ids": sorted(group["assertion_ids"])})

    if not complete:
        status = "TRUNCATED"
    elif not assertions:
        status = "MISSING"
    elif len(groups) > 1:
        status = "AMBIGUOUS"
    elif len(assertions) == 1:
        status = "SINGLE_ASSERTION"
    else:
        status = "EQUIVALENT_ASSERTIONS"

    warnings = list(BASE_WARNINGS)
    if any(row["primary_exchange"] is None for row in assertions):
        warnings.append("Unknown primary exchange remains null; no exchange is inferred.")
    if availability_mode == "reconstructed":
        warnings.append("Reconstructed mode may include later local ingestion and is not evidence of a local forward experiment.")
    if status == "TRUNCATED":
        warnings.append("The supplied assertion set is incomplete; no identity is resolved from a bounded subset.")
    elif status == "MISSING":
        warnings.append("No eligible assertions were supplied; this does not establish that the instrument does not exist.")
    elif status == "AMBIGUOUS":
        warnings.append("Different descriptors or sources remain separate conflicting groups and are not resolved.")
    elif status == "EQUIVALENT_ASSERTIONS":
        warnings.append("Matching provider descriptors remain separate assertions; no synthetic validity interval is created.")

    return {
        "schema_version": SCHEMA_VERSION,
        "instrument_id": instrument_id,
        "as_of": cutoff,
        "availability_mode": availability_mode,
        "complete": complete,
        "status": status,
        "assertion_count": len(assertions),
        "group_count": len(groups),
        "groups": groups,
        "assertions": assertions,
        "resolved_attributes": dict(groups[0]["attributes"])
        if status in {"SINGLE_ASSERTION", "EQUIVALENT_ASSERTIONS"} else None,
        "historical_identity_verified": False,
        "warnings": warnings,
    }


def validate_resolution(result: dict) -> dict:
    """Rebuild an archived result from every raw assertion and compare exactly."""
    try:
        if not isinstance(result, dict) or type(result.get("schema_version")) is not int or result["schema_version"] != SCHEMA_VERSION:
            raise ValueError("unsupported identity resolution schema")
        assertions = result["assertions"]
        if not isinstance(assertions, list) or any(not isinstance(row, dict) for row in assertions):
            raise ValueError("identity resolution assertions are invalid")
        raw_rows = [{key: value for key, value in row.items() if key != "assertion_id"}
                    for row in assertions]
        rebuilt = resolve_identity(raw_rows, instrument_id=result["instrument_id"], as_of=result["as_of"],
                                   availability_mode=result["availability_mode"], complete=result["complete"])
        if canonical_bytes(result) != canonical_bytes(rebuilt):
            raise ValueError("identity resolution does not match its raw assertions")
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("invalid identity resolution or assertion provenance") from None
    return result
