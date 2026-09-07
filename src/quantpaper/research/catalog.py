"""Create-only current-context catalogs; the research warehouse remains read-only."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform
import re

from .evidence import _cell, _identifier, canonical_bytes, digest, utc_timestamp
from .evidence_cli import _create_bytes, _read_bytes
from .identity import resolve_identity, validate_resolution
from .refresh import load_configuration, scoped_database


DEFAULT_SERIES = ("DFF", "DGS10", "CPIAUCSL", "UNRATE")
ARTIFACT_NAMES = frozenset({"catalog.json", "report.md", "manifest.json"})
BUILDER_FILES = ("research/catalog.py", "research/catalog_cli.py", "research/identity.py",
                 "research/evidence.py", "research/evidence_cli.py", "research/evidence_store.py",
                 "research/refresh.py", "sources/fred_metadata.py", "sources/common.py", "sources/http.py")
LIMITATIONS = [
    "Current research context only; no model, signal, order, or trading permission is produced.",
    "Equivalent source descriptions do not verify a historical security or issuer identity.",
    "Every selected identity assertion is retained; truncated samples cannot resolve identity.",
    "Macro metadata is available only after local response observation, not provider last_updated.",
    "The time check additionally requires catalog generation strictly before the decision cutoff.",
    "Current units and frequency do not establish unchanged historical series definitions.",
    "Series frequency is not an exact release schedule, publication timestamp, or trading calendar.",
    "No numeric conversion, resampling, imputation, feature generation, or historical backfill occurs.",
    "The catalog is separate from evidence v1; existing evidence and warehouse rows are not modified.",
    "Provider response hashes identify fetched payloads; raw payloads are not archived for replay.",
    "Local hashes detect ordinary changes, not complete malicious rewriting without a trusted anchor.",
]


def scoped_directory(run_dir: Path, project_root: Path) -> Path:
    root = project_root.resolve()
    raw = run_dir if run_dir.is_absolute() else root / run_dir
    base = root / "artifacts" / "research-catalog"
    if ".." in raw.parts or raw == base or not raw.is_relative_to(base):
        raise ValueError("catalog output must be a dedicated child of artifacts/research-catalog")
    for node in (raw, *raw.parents):
        if node == root:
            break
        if node.is_symlink():
            raise ValueError("catalog paths cannot contain symlinks")
    if not raw.resolve().is_relative_to(base.resolve()):
        raise ValueError("catalog output resolves outside its isolated directory")
    return raw


def _request(instrument_id: str, series: tuple[str, ...] | list[str]) -> dict:
    _identifier(instrument_id, "instrument_id")
    if (not isinstance(series, (tuple, list)) or not 1 <= len(series) <= 4
            or any(not isinstance(item, str) or not re.fullmatch(r"[A-Z0-9_]{1,64}", item, re.ASCII)
                   for item in series) or len(set(series)) != len(series)):
        raise ValueError("request one to four unique canonical macro series")
    return {"instrument_id": instrument_id, "series": list(series)}


def _fingerprint() -> dict:
    root = Path(__file__).resolve().parents[1]
    files = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in BUILDER_FILES}
    return {"files": files, "sha256": digest(files)}


def _read_identity(database: Path, instrument_id: str, as_of: str) -> list[dict]:
    import duckdb
    from .evidence_store import _FIELDS, _require_schema, _rows

    with duckdb.connect(str(database), read_only=True, config={
        "enable_external_access": False, "autoinstall_known_extensions": False,
        "autoload_known_extensions": False,
    }) as connection:
        connection.execute("SET TimeZone='UTC'")
        connection.execute("BEGIN TRANSACTION")
        _require_schema(connection)
        columns = ", ".join(f'i."{name}"' for name in _FIELDS["identity"])
        rows = _rows(connection, "identity", f"""
            SELECT {columns} FROM main.instruments i
            WHERE i.instrument_id = ? AND i.available_at < ?::TIMESTAMPTZ
              AND i.ingested_at < ?::TIMESTAMPTZ AND i.valid_from <= ?::TIMESTAMPTZ
              AND (i.valid_to IS NULL OR i.valid_to > ?::TIMESTAMPTZ)
            ORDER BY i.valid_from DESC, i.source, i.available_at DESC, i.ingested_at DESC
            LIMIT 501
        """, [instrument_id, as_of, as_of, as_of, as_of])
        connection.execute("COMMIT")
    return rows


def _hash(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value, re.ASCII) is not None


def _metadata(outcome: dict, series: str, started: str, generated: str) -> None:
    from ..sources.fred_metadata import validate_metadata_record

    if not isinstance(outcome, dict) or outcome.get("series_id") != series:
        raise ValueError("catalog series identity mismatch")
    status = outcome.get("status")
    if status == "AVAILABLE":
        if set(outcome) != {"series_id", "status", "request", "record", "record_hash", "content_hash"}:
            raise ValueError("unexpected metadata receipt fields")
        record = validate_metadata_record(outcome["record"])
        if record["series_id"] != series or outcome["record_hash"] != digest(record) or not _hash(outcome["content_hash"]):
            raise ValueError("catalog metadata provenance mismatch")
        observed = utc_timestamp(record["observed_at"])
        if not started <= observed <= generated:
            raise ValueError("metadata observation falls outside this catalog run")
        request = outcome["request"]
        expected = {"series_id": series, "file_type": "json",
                    "realtime_start": record["realtime_start"], "realtime_end": record["realtime_end"]}
        if request != expected or not started[:10] <= record["realtime_start"] <= observed[:10]:
            raise ValueError("metadata request does not match the current observation")
    elif status in {"MISSING_CONFIGURATION", "INVALID_CONFIGURATION", "FAILED"}:
        if set(outcome) != {"series_id", "status", "reason"} or outcome["reason"] != (
            "Metadata source failed validation or transport; no fallback was used." if status == "FAILED"
            else "Configure FRED locally; no metadata request was sent."
        ):
            raise ValueError("invalid metadata failure receipt")
    else:
        raise ValueError("unsupported metadata status")


def validate_catalog(catalog: dict) -> dict:
    if not isinstance(catalog, dict) or set(catalog) != {
        "schema_version", "request", "started_at", "generated_at", "identity", "macro_metadata",
        "status", "research_only", "execution_enabled", "warehouse_modified", "model_changed",
        "limitations", "catalog_hash",
    }:
        raise ValueError("invalid catalog fields")
    if type(catalog["schema_version"]) is not int or catalog["schema_version"] != 1:
        raise ValueError("unsupported catalog schema")
    if catalog["catalog_hash"] != digest({k: v for k, v in catalog.items() if k != "catalog_hash"}):
        raise ValueError("catalog hash mismatch")
    for name, expected in (("research_only", True), ("execution_enabled", False),
                           ("warehouse_modified", False), ("model_changed", False)):
        if catalog[name] is not expected:
            raise ValueError("invalid catalog safety flags")
    if catalog["limitations"] != LIMITATIONS:
        raise ValueError("catalog limitations changed")
    request = catalog["request"]
    if not isinstance(request, dict) or _request(**request) != request:
        raise ValueError("catalog request mismatch")
    started, generated = (utc_timestamp(catalog[name]) for name in ("started_at", "generated_at"))
    if started != catalog["started_at"] or generated != catalog["generated_at"] or started > generated:
        raise ValueError("invalid catalog clock")
    identity = validate_resolution(catalog["identity"])
    if (identity["instrument_id"] != request["instrument_id"] or identity["as_of"] != started
            or identity["availability_mode"] != "local_observed" or identity["assertion_count"] > 501
            or identity["complete"] is not (identity["assertion_count"] <= 500)):
        raise ValueError("identity snapshot scope or overflow mismatch")
    metadata = catalog["macro_metadata"]
    if not isinstance(metadata, dict) or set(metadata) != set(request["series"]):
        raise ValueError("metadata series list mismatch")
    for series, outcome in metadata.items():
        _metadata(outcome, series, started, generated)
    complete = (identity["status"] in {"SINGLE_ASSERTION", "EQUIVALENT_ASSERTIONS"}
                and all(item["status"] == "AVAILABLE" for item in metadata.values()))
    if catalog["status"] != ("COMPLETE" if complete else "PARTIAL"):
        raise ValueError("catalog aggregate status mismatch")
    return catalog


def build_catalog(database: Path, run_dir: Path, *, project_root: Path, instrument_id: str = "YF:JPM",
                  series: tuple[str, ...] = DEFAULT_SERIES, configuration=None, client_factory=None,
                  now=None) -> dict:
    """Observe current metadata and preserve an independent identity snapshot.

    Factories and clocks are injectable for offline tests. No write connection,
    broker, model, initializer, or existing evidence artifact is used.
    """
    request = _request(instrument_id, series)
    directory = scoped_directory(run_dir, project_root)
    if directory.exists():
        raise ValueError("catalog directory already exists; use a new name")
    database = scoped_database(database, project_root)
    builder = _fingerprint()
    clock = now or (lambda: datetime.now(timezone.utc))
    started = utc_timestamp(clock())
    rows = _read_identity(database, instrument_id, started)
    identity = resolve_identity(rows, instrument_id=instrument_id, as_of=started,
                                availability_mode="local_observed", complete=len(rows) <= 500)
    config = configuration if configuration is not None else load_configuration(project_root)
    readiness = config.readiness()["fred"]
    metadata = {}
    for series_id in request["series"]:
        if readiness != "READY":
            metadata[series_id] = {"series_id": series_id, "status": readiness,
                                   "reason": "Configure FRED locally; no metadata request was sent."}
            continue
        try:
            if client_factory is None:
                from ..sources.fred_metadata import FREDSeriesMetadataClient
                client_factory = FREDSeriesMetadataClient
            batch = client_factory(config.fred_api_key).fetch(series_id)
            if batch.source != "fred-series-metadata" or len(batch.records) != 1:
                raise ValueError("metadata fetch must identify exactly one series")
            outcome = {"series_id": series_id, "status": "AVAILABLE", "request": batch.request,
                       "record": batch.records[0], "record_hash": digest(batch.records[0]),
                       "content_hash": batch.content_hash}
            _metadata(outcome, series_id, started, utc_timestamp(clock()))
            metadata[series_id] = outcome
        except Exception:
            metadata[series_id] = {"series_id": series_id, "status": "FAILED",
                                   "reason": "Metadata source failed validation or transport; no fallback was used."}
    catalog = {"schema_version": 1, "request": request, "started_at": started,
               "generated_at": utc_timestamp(clock()), "identity": identity, "macro_metadata": metadata,
               "status": "COMPLETE" if identity["status"] in {"SINGLE_ASSERTION", "EQUIVALENT_ASSERTIONS"}
               and all(item["status"] == "AVAILABLE" for item in metadata.values()) else "PARTIAL",
               "research_only": True, "execution_enabled": False, "warehouse_modified": False,
               "model_changed": False, "limitations": list(LIMITATIONS)}
    catalog["catalog_hash"] = digest(catalog)
    validate_catalog(catalog)
    if builder != _fingerprint():
        raise ValueError("catalog builder changed during collection")
    manifest = {"schema_version": 1, "builder": builder, "catalog_hash": catalog["catalog_hash"],
                "runtime": {"python": platform.python_version(), "duckdb": version("duckdb"),
                            "certifi": version("certifi")},
                "research_only": True, "execution_enabled": False}
    manifest["manifest_hash"] = digest(manifest)
    payloads = {"catalog.json": canonical_bytes(catalog) + b"\n",
                "report.md": render_markdown(catalog).encode(),
                "manifest.json": canonical_bytes(manifest) + b"\n"}
    if any(len(payload) > 20_000_000 for payload in payloads.values()):
        raise ValueError("catalog archive exceeds the size budget")
    scoped_directory(directory, project_root)
    directory.mkdir(parents=True, exist_ok=False)
    for name, payload in payloads.items():
        _create_bytes(directory / name, payload)
    _create_bytes(directory / "completion.json", canonical_bytes({
        "schema_version": 1, "status": "complete", "catalog_hash": catalog["catalog_hash"],
        "manifest_hash": manifest["manifest_hash"],
        "artifacts": {name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()},
    }) + b"\n")
    return catalog


def read_catalog(run_dir: Path, *, project_root: Path) -> dict:
    directory = scoped_directory(run_dir, project_root)
    completion = json.loads(_read_bytes(directory / "completion.json"))
    if (not isinstance(completion, dict) or type(completion.get("schema_version")) is not int
            or completion["schema_version"] != 1 or completion.get("status") != "complete"
            or not isinstance(completion.get("artifacts"), dict)
            or set(completion["artifacts"]) != ARTIFACT_NAMES):
        raise ValueError("catalog archive is incomplete or unsupported")
    payloads = {name: _read_bytes(directory / name) for name in ARTIFACT_NAMES}
    if any(hashlib.sha256(payload).hexdigest() != completion["artifacts"][name]
           for name, payload in payloads.items()):
        raise ValueError("catalog artifact hash mismatch")
    catalog = validate_catalog(json.loads(payloads["catalog.json"]))
    manifest = json.loads(payloads["manifest.json"])
    if not isinstance(manifest, dict):
        raise ValueError("catalog manifest must be an object")
    expected_hash = digest({k: v for k, v in manifest.items() if k != "manifest_hash"})
    if (manifest.get("manifest_hash") != expected_hash or completion.get("manifest_hash") != expected_hash
            or manifest.get("catalog_hash") != catalog["catalog_hash"]
            or completion.get("catalog_hash") != catalog["catalog_hash"]):
        raise ValueError("catalog manifest linkage mismatch")
    if (type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1
            or manifest.get("research_only") is not True or manifest.get("execution_enabled") is not False):
        raise ValueError("invalid catalog manifest flags")
    builder = manifest.get("builder")
    if (not isinstance(builder, dict) or not isinstance(builder.get("files"), dict)
            or set(builder["files"]) != set(BUILDER_FILES) or not all(_hash(value) for value in builder["files"].values())
            or builder.get("sha256") != digest(builder["files"])):
        raise ValueError("invalid catalog builder fingerprint")
    if payloads["report.md"] != render_markdown(catalog).encode():
        raise ValueError("catalog display report differs from validated data")
    return catalog


def metadata_as_of(catalog: dict, as_of: str) -> dict:
    """Return only locally available metadata; this does not build trading features."""
    validate_catalog(catalog)
    cutoff = utc_timestamp(as_of)
    results = {}
    for series, outcome in catalog["macro_metadata"].items():
        record = outcome.get("record")
        known = (outcome["status"] == "AVAILABLE" and catalog["generated_at"] < cutoff
                 and utc_timestamp(record["available_at"]) < cutoff)
        results[series] = {"status": "AVAILABLE" if known else "UNAVAILABLE_AT_CUTOFF",
                           "record": record.copy() if known else None}
    return {"catalog_hash": catalog["catalog_hash"], "as_of": cutoff, "macro_metadata": results,
            "research_only": True, "execution_enabled": False,
            "identity_reinterpreted": False, "features_generated": False}


def summary(catalog: dict) -> dict:
    validate_catalog(catalog)
    return {"catalog_hash": catalog["catalog_hash"], "status": catalog["status"],
            "identity_status": catalog["identity"]["status"],
            "identity_assertions": catalog["identity"]["assertion_count"],
            "identity_groups": catalog["identity"]["group_count"],
            "macro_metadata": {key: value["status"] for key, value in catalog["macro_metadata"].items()},
            "research_only": True, "execution_enabled": False, "warehouse_modified": False}


def render_markdown(catalog: dict) -> str:
    validate_catalog(catalog)
    identity = catalog["identity"]
    lines = ["# Research Data Catalog", "", "Research context only. No feature, prediction, or order was generated.",
             "", f"- Status: {_cell(catalog['status'])}", f"- Identity cutoff: {_cell(catalog['started_at'])}",
             f"- Catalog generated: {_cell(catalog['generated_at'])}", f"- Catalog SHA-256: {catalog['catalog_hash']}",
             "", "## Source identity assertions", "", f"- Instrument: {_cell(identity['instrument_id'])}",
             f"- Result: {_cell(identity['status'])}", f"- Assertions retained: {identity['assertion_count']}",
             f"- Distinct source descriptions: {identity['group_count']}",
             "- Historical security identity verified: False", "",
             *[f"- {_cell(warning)}" for warning in identity["warnings"]],
             "", "## Current macro metadata", "",
             "| Series | Status | Units | Frequency | Seasonal adjustment | Observed at |",
             "| --- | --- | --- | --- | --- | --- |"]
    for series in catalog["request"]["series"]:
        outcome = catalog["macro_metadata"][series]
        record = outcome.get("record", {})
        lines.append("| " + " | ".join(_cell(value) for value in (
            series, outcome["status"], record.get("units"), record.get("frequency"),
            record.get("seasonal_adjustment"), record.get("observed_at"))) + " |")
    lines.extend(["", "## Limitations", "", *[f"- {item}" for item in catalog["limitations"]], ""])
    return "\n".join(lines)
