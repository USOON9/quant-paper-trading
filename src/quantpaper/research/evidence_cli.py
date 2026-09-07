"""Read-only warehouse input and create-only, locally isolated evidence output."""

from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import stat
import sys
import tempfile

from .evidence import (DEFAULT_SERIES, MODES, EvidenceRequest, build_evidence_packet,
                       digest, packet_summary, render_markdown, validate_packet)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
ARTIFACT_NAMES = frozenset({"packet.json", "report.md", "manifest.json"})
MAX_ARTIFACT_BYTES = 20_000_000


def scoped_directory(run_dir: Path, project_root: Path = PROJECT_ROOT) -> Path:
    root = project_root.resolve()
    raw = run_dir if run_dir.is_absolute() else root / run_dir
    base = root / "artifacts" / "research-evidence"
    if ".." in raw.parts or raw == base or not raw.is_relative_to(base):
        raise ValueError("run directory must be a dedicated child of artifacts/research-evidence")
    for node in (raw, *raw.parents):
        if node == root:
            break
        if node.is_symlink():
            raise ValueError("evidence paths must not contain symlinks")
    if not raw.resolve().is_relative_to(base.resolve()):
        raise ValueError("evidence output resolves outside its isolated directory")
    return raw


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def _create_bytes(path: Path, payload: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _code_fingerprint() -> dict:
    directory = Path(__file__).resolve().parent
    files = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
             for name in ("evidence.py", "evidence_store.py", "evidence_cli.py")}
    return {"files": files, "sha256": digest(files)}


def build_run(database: Path, request: EvidenceRequest, run_dir: Path,
              *, project_root: Path = PROJECT_ROOT) -> dict:
    directory = scoped_directory(run_dir, project_root)
    if directory.exists():
        raise ValueError("run directory already exists; use a new name to preserve evidence")
    code = _code_fingerprint()
    packet = build_evidence_packet(database, request)
    report = render_markdown(packet)
    if code != _code_fingerprint():
        raise ValueError("evidence builder code changed during the run")
    manifest = {
        "schema_version": 1, "builder": code,
        "runtime": {"python": platform.python_version(), "duckdb": version("duckdb")},
        "packet_hash": packet["packet_hash"], "snapshot_hash": packet["snapshot_hash"],
        "request_hash": digest(packet["request"]),
        "input": "Selected rows from one read-only DuckDB transaction; not a full database backup.",
        "research_only": True, "execution_enabled": False,
    }
    manifest["manifest_hash"] = digest(manifest)
    payloads = {"packet.json": _json_bytes(packet), "report.md": report.encode(),
                "manifest.json": _json_bytes(manifest)}
    if any(len(payload) > MAX_ARTIFACT_BYTES for payload in payloads.values()):
        raise ValueError("evidence output exceeds artifact size limit; reduce max_records")
    # Revalidate before exclusively creating ownership of a new run.
    scoped_directory(directory, project_root)
    directory.mkdir(parents=True, exist_ok=False)
    for name, payload in payloads.items():
        _create_bytes(directory / name, payload)
    # A partial directory is preserved on failure and cannot masquerade as complete.
    _create_bytes(directory / "completion.json", _json_bytes({
        "schema_version": 1, "status": "complete", "packet_hash": packet["packet_hash"],
        "manifest_hash": manifest["manifest_hash"],
        "artifacts": {name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()},
    }))
    return packet


def _read_bytes(path: Path) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_BYTES:
        raise ValueError("evidence artifact must be a bounded regular file, not a symlink")
    return path.read_bytes()


def read_run(run_dir: Path, *, project_root: Path = PROJECT_ROOT) -> dict:
    directory = scoped_directory(run_dir, project_root)
    completion = json.loads(_read_bytes(directory / "completion.json"))
    if not isinstance(completion, dict) or not isinstance(completion.get("artifacts"), dict):
        raise ValueError("evidence completion must be an object with an artifact map")
    if type(completion.get("schema_version")) is not int or completion.get("schema_version") != 1 or completion.get("status") != "complete":
        raise ValueError("evidence run is incomplete or unsupported")
    if set(completion.get("artifacts", {})) != ARTIFACT_NAMES:
        raise ValueError("unexpected evidence artifact list")
    payloads = {name: _read_bytes(directory / name) for name in sorted(ARTIFACT_NAMES)}
    for name, payload in payloads.items():
        if hashlib.sha256(payload).hexdigest() != completion["artifacts"][name]:
            raise ValueError(f"evidence artifact integrity mismatch: {name}")
    packet = validate_packet(json.loads(payloads["packet.json"]))
    manifest = json.loads(payloads["manifest.json"])
    if not isinstance(manifest, dict):
        raise ValueError("evidence manifest must be an object")
    expected = digest({key: value for key, value in manifest.items() if key != "manifest_hash"})
    if manifest.get("manifest_hash") != expected or completion.get("manifest_hash") != expected:
        raise ValueError("evidence manifest hash mismatch")
    if (manifest.get("packet_hash") != packet["packet_hash"] or
            completion.get("packet_hash") != packet["packet_hash"] or
            manifest.get("snapshot_hash") != packet["snapshot_hash"] or
            manifest.get("request_hash") != digest(packet["request"])):
        raise ValueError("evidence manifest does not match the packet")
    if type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 1 or manifest.get("research_only") is not True or manifest.get("execution_enabled") is not False:
        raise ValueError("invalid evidence manifest schema or safety flags")
    builder = manifest.get("builder")
    if not isinstance(builder, dict) or not isinstance(builder.get("files"), dict):
        raise ValueError("evidence builder fingerprint must be an object")
    if set(builder["files"]) != {"evidence.py", "evidence_store.py", "evidence_cli.py"}:
        raise ValueError("unexpected evidence builder file list")
    if builder["sha256"] != digest(builder["files"]):
        raise ValueError("evidence builder fingerprint mismatch")
    return packet


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline, non-executable research evidence packets")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Read an existing warehouse and create a new evidence run")
    build.add_argument("--db", type=Path, default=Path("data/research.duckdb"))
    build.add_argument("--instrument-id", required=True)
    build.add_argument("--fundamental-instrument-id")
    build.add_argument("--news-entity")
    build.add_argument("--series", nargs="*", default=list(DEFAULT_SERIES))
    build.add_argument("--as-of", required=True, help="Exclusive ISO 8601 cutoff with explicit timezone")
    build.add_argument("--availability-mode", choices=MODES, default="local_observed")
    build.add_argument("--max-records", type=int, default=100, help="Limit per section and per macro series (1..500)")
    build.add_argument("--run-dir", type=Path, required=True)
    show = commands.add_parser("show", help="Verify archived artifact hashes without opening a database")
    show.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "show":
            packet = read_run(args.run_dir)
        else:
            request = EvidenceRequest(
                instrument_id=args.instrument_id, as_of=args.as_of,
                fundamental_instrument_id=args.fundamental_instrument_id,
                news_entity=args.news_entity, macro_series=tuple(args.series),
                availability_mode=args.availability_mode, max_records=args.max_records,
            )
            packet = build_run(args.db, request, args.run_dir)
        print(json.dumps(packet_summary(packet), indent=2, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        # Do not echo raw database content, paths, provider URLs, or parser payloads.
        print("Evidence command failed: check the database schema, explicit identifiers, timezone-aware cutoff, "
              "and a new isolated run directory. For show, verify the run is complete and unmodified.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
