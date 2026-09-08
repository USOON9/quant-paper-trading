"""Auditable as-of context features from copied evidence and observed metadata.

This module never trains or loads a model, fetches a provider, reads credentials,
or writes the warehouse. Existing evidence/catalog v1 formats remain unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform
import re

from .catalog import metadata_as_of, read_catalog, validate_catalog
from .evidence import (EvidenceRequest, _cell, build_evidence_packet, canonical_bytes, digest,
                       utc_timestamp, validate_packet as validate_evidence)
from .evidence_cli import _create_bytes, _read_bytes
from .feature_math import CONTRACT_ID, CORE_SERIES, FEATURE_NAMES, compute_features, feature_contract
from .identity import resolve_identity
from .refresh import scoped_database


ARTIFACT_NAMES = frozenset({"features.json", "evidence.json", "catalog.json", "report.md", "manifest.json"})
BUILDER_FILES = ("research/feature_packet.py", "research/feature_math.py", "research/feature_cli.py",
                 "research/evidence.py", "research/evidence_store.py", "research/evidence_cli.py",
                 "research/catalog.py", "research/identity.py", "research/refresh.py",
                 "sources/fred_metadata.py", "sources/common.py", "sources/http.py")
LIMITATIONS = [
    "Research context only: no label, probability, signal, model admission, order, or training is produced.",
    "This is a newly computed as-of reconstruction, not proof of a feature or prediction emitted at that past cutoff.",
    "Source availability and local ingestion must both be strictly before the decision cutoff.",
    "Metadata and its catalog must be observed/generated strictly before cutoff; current definitions are never backdated.",
    "Observed-bar horizons are not verified consecutive exchange sessions; no trading calendar is inferred.",
    "Quality age/gap thresholds are frozen research assumptions, not execution freshness or exchange-session gates.",
    "Macro levels retain provider units. Current definitions do not prove unchanged historical units across revisions.",
    "Latest missing values remain missing; there is no fallback, filling, resampling, scaling, or imputation.",
    "A complete row means all eleven bounded calculations passed this contract, not complete economic coverage or model readiness.",
    "Historical bars downloaded later are not historical local-observed training decision rows.",
    "Identity binding checks matching source descriptions only; historical security identity remains unverified.",
    "SEC fundamentals and news are not requested: cross-issuer joins, accounting normalization, and licensed text need separate contracts.",
    "Archived inputs support offline replay; raw provider responses and the full warehouse are not copied.",
    "Hashes detect ordinary changes, not coordinated rewriting without an external trusted anchor.",
]


def scoped_directory(run_dir: Path, project_root: Path) -> Path:
    root = project_root.resolve()
    raw = run_dir if run_dir.is_absolute() else root / run_dir
    base = root / "artifacts" / "research-features"
    if ".." in raw.parts or raw == base or not raw.is_relative_to(base):
        raise ValueError("feature run must be a dedicated child of artifacts/research-features")
    for node in (raw, *raw.parents):
        if node == root:
            break
        if node.is_symlink():
            raise ValueError("feature paths cannot contain symlinks")
    if not raw.resolve().is_relative_to(base.resolve()):
        raise ValueError("feature output resolves outside its isolated directory")
    return raw


def _fingerprint() -> dict:
    root = Path(__file__).resolve().parents[1]
    files = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in BUILDER_FILES}
    return {"files": files, "sha256": digest(files)}


def _binding(evidence: dict, catalog: dict, as_of: str) -> dict:
    section = evidence["sections"]["identity"]
    rows = [{k: v for k, v in row.items() if k != "evidence_id"} for row in section["records"]]
    current = resolve_identity(rows, instrument_id=evidence["request"]["instrument_id"], as_of=as_of,
                               availability_mode="local_observed", complete=not section["truncated"])
    prior = catalog["identity"]
    supported = {"SINGLE_ASSERTION", "EQUIVALENT_ASSERTIONS"}
    if catalog["generated_at"] >= as_of:
        status = "CATALOG_UNAVAILABLE_AT_CUTOFF"
    elif current["status"] not in supported:
        status = "CURRENT_IDENTITY_UNRESOLVED"
    elif prior["status"] not in supported:
        status = "CATALOG_IDENTITY_UNRESOLVED"
    elif current["resolved_attributes"] != prior["resolved_attributes"]:
        status = "SOURCE_DESCRIPTION_MISMATCH"
    else:
        attributes = current["resolved_attributes"]
        status = ("MATCHED_SOURCE_DESCRIPTION" if attributes["source"] == "yahoo"
                  and attributes["asset_class"] == "equity"
                  and attributes["instrument_id"] == f"YF:{attributes['symbol']}"
                  else "UNSUPPORTED_SOURCE_OR_ASSET")
    return {"status": status, "current_resolution": current,
            "catalog_resolution_status": prior["status"],
            "historical_identity_verified": False}


def _coverage(evidence: dict) -> dict:
    sections = evidence["sections"]
    result = {}
    for name, section in [("identity", sections["identity"]), ("market", sections["market"]),
                          *[(f"macro:{s}", sections["macro"][s]) for s in CORE_SERIES]]:
        rows = section["records"]
        date_field = "event_time" if name == "market" else "observation_date" if name.startswith("macro:") else "valid_from"
        result[name] = {key: section[key] for key in ("status", "count", "truncated", "latest_available_at")}
        result[name]["latest_event_or_observation"] = max((row[date_field] for row in rows), default=None)
    return result


def build_packet(evidence: dict, catalog: dict, *, generated_at: datetime | str | None = None) -> dict:
    """Pure calculation over already archived/validated input objects."""
    validate_evidence(evidence)
    validate_catalog(catalog)
    request = evidence["request"]
    if (request["availability_mode"] != "local_observed" or request["fundamental_instrument_id"] is not None
            or request["news_entity"] is not None or request["max_records"] != 500
            or set(request["macro_series"]) != set(CORE_SERIES)
            or set(catalog["request"]["series"]) != set(CORE_SERIES)
            or request["instrument_id"] != catalog["request"]["instrument_id"]):
        raise ValueError("feature inputs do not match the fixed local-observed scope")
    as_of = request["as_of"]
    generated = utc_timestamp(generated_at or datetime.now(timezone.utc))
    if generated < max(as_of, utc_timestamp(evidence["generated_at"]), catalog["generated_at"]):
        raise ValueError("feature generation precedes its inputs or decision cutoff")
    binding = _binding(evidence, catalog, as_of)
    selected_metadata = metadata_as_of(catalog, as_of)["macro_metadata"]
    metadata = {series: selected_metadata[series]["record"] for series in CORE_SERIES}
    sections = evidence["sections"]
    results = compute_features(sections["market"]["records"],
                               {series: sections["macro"][series]["records"] for series in CORE_SERIES},
                               metadata, as_of=as_of)
    if set(results) != set(FEATURE_NAMES):
        raise ValueError("feature math returned an unexpected contract")
    market_quality = {name: result["status"] for name, result in results.items() if name.startswith("market.")}
    if binding["status"] != "MATCHED_SOURCE_DESCRIPTION":
        for name, result in results.items():
            if name.startswith("market."):
                results[name] = {**result, "status": "IDENTITY_BLOCKED", "value": None,
                                 "reason": "No eligible matching single-source equity description binds these inputs."}
    contract = feature_contract()
    packet = {"schema_version": 1, "contract_id": CONTRACT_ID, "contract": contract,
              "contract_hash": digest(contract), "generated_at": generated,
              "request": {"instrument_id": request["instrument_id"], "as_of": as_of,
                          "availability_mode": "local_observed"},
              "input_hashes": {"evidence_packet_hash": evidence["packet_hash"],
                               "evidence_snapshot_hash": evidence["snapshot_hash"],
                               "catalog_hash": catalog["catalog_hash"]},
              "identity_binding": binding, "unmasked_market_quality": market_quality,
              "metadata_eligibility": {s: selected_metadata[s]["status"] for s in CORE_SERIES},
              "source_coverage": _coverage(evidence), "features": results,
              "status": "COMPLETE" if all(value["status"] == "AVAILABLE" for value in results.values()) else "PARTIAL",
              "feature_count": len(FEATURE_NAMES),
              "available_count": sum(value["status"] == "AVAILABLE" for value in results.values()),
              "research_only": True, "execution_enabled": False, "model_changed": False,
              "training_ready": False, "prediction_generated": False, "historical_forward_sample": False,
              "warehouse_modified": False, "limitations": list(LIMITATIONS)}
    packet["feature_hash"] = digest({"request": packet["request"], "contract_hash": packet["contract_hash"],
                                     "input_hashes": packet["input_hashes"], "identity_binding": binding,
                                     "features": results})
    packet["packet_hash"] = digest(packet)
    return packet


def validate_packet(packet: dict, evidence: dict, catalog: dict) -> dict:
    if (not isinstance(packet, dict) or type(packet.get("schema_version")) is not int
            or packet["schema_version"] != 1 or packet.get("contract_id") != CONTRACT_ID):
        raise ValueError("unsupported feature packet schema or contract")
    rebuilt = build_packet(evidence, catalog, generated_at=packet["generated_at"])
    if canonical_bytes(packet) != canonical_bytes(rebuilt):
        raise ValueError("feature packet differs from recomputed archived inputs")
    return packet


def packet_summary(packet: dict) -> dict:
    return {key: packet[key] for key in ("packet_hash", "feature_hash", "contract_id", "status", "request",
                                        "available_count", "feature_count", "research_only", "execution_enabled",
                                        "training_ready", "model_changed")}


def render_markdown(packet: dict) -> str:
    lines = ["# As-of Research Feature Packet", "",
             "Research context only. No label, prediction, trade, or model approval was generated.", "",
             f"- Instrument: {_cell(packet['request']['instrument_id'])}",
             f"- Exclusive decision cutoff: {_cell(packet['request']['as_of'])}",
             f"- Computed at: {_cell(packet['generated_at'])}",
             f"- Contract: {_cell(packet['contract_id'])}", f"- Packet SHA-256: {packet['packet_hash']}",
             f"- Status: {packet['status']} ({packet['available_count']}/{packet['feature_count']} available)",
             f"- Identity binding: {_cell(packet['identity_binding']['status'])}",
             "- Training ready: False", "", "## Features", "",
             "| Feature | Status | Value | Unit | Latest input availability | Reason |",
             "| --- | --- | ---: | --- | --- | --- |"]
    for name in FEATURE_NAMES:
        result = packet["features"][name]
        lines.append("| " + " | ".join(_cell(value) for value in (
            name, result["status"], result["value"], result["unit"],
            result["latest_input_available_at"], result["reason"])) + " |")
    lines.extend(["", "## Source coverage", "",
                  "| Section | Source status | Records retained | Truncated | Latest event / observation |",
                  "| --- | --- | ---: | --- | --- |"])
    for name in sorted(packet["source_coverage"]):
        coverage = packet["source_coverage"][name]
        lines.append("| " + " | ".join(_cell(value) for value in (
            name, coverage["status"], coverage["count"], coverage["truncated"],
            coverage["latest_event_or_observation"])) + " |")
    lines.extend(["", "Full evidence IDs, metadata hashes, input snapshots, and formulas are retained in the JSON archive.",
                  "", "## Limitations", "", *[f"- {item}" for item in packet["limitations"]], ""])
    return "\n".join(lines)


def build_run(database: Path, catalog_run_dir: Path, run_dir: Path, *, project_root: Path,
              instrument_id: str, as_of: str) -> dict:
    request = EvidenceRequest(instrument_id=instrument_id, as_of=as_of, macro_series=CORE_SERIES,
                              availability_mode="local_observed", max_records=500)
    directory = scoped_directory(run_dir, project_root)
    if directory.exists():
        raise ValueError("feature archive already exists; use a new directory")
    database = scoped_database(database, project_root)
    builder = _fingerprint()
    catalog = read_catalog(catalog_run_dir, project_root=project_root)
    if request.instrument_id != catalog["request"]["instrument_id"] or set(catalog["request"]["series"]) != set(CORE_SERIES):
        raise ValueError("catalog scope does not match the requested feature contract")
    evidence = build_evidence_packet(database, request)
    packet = build_packet(evidence, catalog)
    validate_packet(packet, evidence, catalog)
    manifest = {"schema_version": 1, "builder": builder,
                "runtime": {"python": platform.python_version(), "duckdb": version("duckdb")},
                "packet_hash": packet["packet_hash"], "feature_hash": packet["feature_hash"],
                "contract_hash": packet["contract_hash"], "input_hashes": packet["input_hashes"],
                "research_only": True, "execution_enabled": False, "warehouse_modified": False}
    manifest["manifest_hash"] = digest(manifest)
    payloads = {"features.json": canonical_bytes(packet) + b"\n", "evidence.json": canonical_bytes(evidence) + b"\n",
                "catalog.json": canonical_bytes(catalog) + b"\n", "report.md": render_markdown(packet).encode(),
                "manifest.json": canonical_bytes(manifest) + b"\n"}
    if any(len(payload) > 20_000_000 for payload in payloads.values()):
        raise ValueError("feature artifact exceeds the size budget")
    if builder != _fingerprint():
        raise ValueError("feature builder changed during the run")
    scoped_directory(directory, project_root)
    directory.mkdir(parents=True, exist_ok=False)
    for name, payload in payloads.items():
        _create_bytes(directory / name, payload)
    _create_bytes(directory / "completion.json", canonical_bytes({
        "schema_version": 1, "status": "complete", "packet_hash": packet["packet_hash"],
        "manifest_hash": manifest["manifest_hash"],
        "artifacts": {name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()},
    }) + b"\n")
    return packet


def read_run(run_dir: Path, *, project_root: Path) -> dict:
    directory = scoped_directory(run_dir, project_root)
    completion = json.loads(_read_bytes(directory / "completion.json"))
    if (not isinstance(completion, dict) or type(completion.get("schema_version")) is not int
            or completion["schema_version"] != 1 or completion.get("status") != "complete"
            or not isinstance(completion.get("artifacts"), dict) or set(completion["artifacts"]) != ARTIFACT_NAMES):
        raise ValueError("feature run is incomplete or unsupported")
    payloads = {name: _read_bytes(directory / name) for name in ARTIFACT_NAMES}
    if any(hashlib.sha256(payload).hexdigest() != completion["artifacts"][name]
           for name, payload in payloads.items()):
        raise ValueError("feature artifact hash mismatch")
    evidence, catalog = (json.loads(payloads[name]) for name in ("evidence.json", "catalog.json"))
    packet = validate_packet(json.loads(payloads["features.json"]), evidence, catalog)
    if payloads["report.md"] != render_markdown(packet).encode():
        raise ValueError("feature report differs from validated data")
    manifest = json.loads(payloads["manifest.json"])
    if not isinstance(manifest, dict):
        raise ValueError("feature manifest must be an object")
    expected = digest({k: v for k, v in manifest.items() if k != "manifest_hash"})
    if (manifest.get("manifest_hash") != expected or completion.get("manifest_hash") != expected
            or completion.get("packet_hash") != packet["packet_hash"]
            or any(manifest.get(key) != packet[key] for key in ("packet_hash", "feature_hash", "contract_hash", "input_hashes"))):
        raise ValueError("feature manifest linkage mismatch")
    if (type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1
            or manifest.get("research_only") is not True or manifest.get("execution_enabled") is not False
            or manifest.get("warehouse_modified") is not False):
        raise ValueError("feature manifest schema or safety flags mismatch")
    builder = manifest.get("builder")
    if (not isinstance(builder, dict) or not isinstance(builder.get("files"), dict)
            or set(builder["files"]) != set(BUILDER_FILES)
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value, re.ASCII)
                   for value in builder["files"].values())
            or builder.get("sha256") != digest(builder["files"])):
        raise ValueError("feature builder fingerprint mismatch")
    return packet
