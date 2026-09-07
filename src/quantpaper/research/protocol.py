"""Validated research specification and create-only snapshot manifests.

Manifests are integrity checked, not digitally signed. They record an offline
research experiment and cannot authorize trading or automatic model promotion.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
from importlib import metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import tempfile
import tomllib
from typing import Any


PROTOCOL_KEYS = {
    "protocol_id", "target_contract_version", "research_only", "executable_backtest",
    "promotion_disabled", "historical_cutoff", "forward_start", "position_policy",
    "long_threshold", "models", "validation", "groups",
}
MODELS = ["base_rate", "logistic", "hist_gradient_boosting"]
UNIVERSES = {"equities": {"SPY", "JPM", "XOM", "WMT", "JNJ"}, "crypto": {"BTC-USD"}}
MANIFEST_KEYS = {
    "manifest_version", "created_at", "research_only", "executable_backtest",
    "promotion_disabled", "protocol", "protocol_hash", "input_files", "code_files",
    "runtime", "snapshot_hash", "manifest_hash",
}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _shape(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} must contain exactly {sorted(keys)}")
    return value


def _number(value: object, label: str, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return float(value)


def _day(value: object, label: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{label} must be a quoted YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{label} is not a valid date") from None


def validate_protocol(protocol: object) -> dict[str, Any]:
    """Return a detached canonical dict, rejecting unknown or unsafe settings."""
    result = _shape(protocol, PROTOCOL_KEYS, "protocol")
    identifier = result["protocol_id"]
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-z][a-z0-9_]{5,79}", identifier):
        raise ValueError("protocol_id must be a safe lowercase identifier")
    if result["target_contract_version"] != "daily_timing_v3":
        raise ValueError("Unsupported target_contract_version")
    if result["research_only"] is not True or result["promotion_disabled"] is not True:
        raise ValueError("research_only and promotion_disabled must remain true")
    if result["executable_backtest"] is not False:
        raise ValueError("executable_backtest must remain false")
    if result["position_policy"] != "long_flat":
        raise ValueError("Only the long_flat position policy is supported")
    cutoff = _day(result["historical_cutoff"], "historical_cutoff")
    forward = _day(result["forward_start"], "forward_start")
    if cutoff >= forward:
        raise ValueError("historical_cutoff must precede forward_start")
    threshold = _number(result["long_threshold"], "long_threshold", 0.5, 1.0)
    if threshold in (0.5, 1.0):
        raise ValueError("long_threshold must be strictly between 0.5 and 1")
    if result["models"] != MODELS:
        raise ValueError(f"models must be the prespecified comparison {MODELS}")
    validation = _shape(result["validation"], {
        "min_train_sessions", "test_sessions", "purge_sessions", "max_folds",
    }, "validation")
    for key, value in validation.items():
        if type(value) is not int or not 1 <= value <= 100_000:
            raise ValueError(f"validation.{key} must be a positive bounded integer")
    if validation["min_train_sessions"] <= validation["purge_sessions"]:
        raise ValueError("min_train_sessions must exceed purge_sessions")
    if validation["max_folds"] > 100:
        raise ValueError("max_folds must not exceed 100")
    groups = _shape(result["groups"], set(UNIVERSES), "groups")
    for name, universe in UNIVERSES.items():
        group = _shape(groups[name], {"symbols", "costs_bps", "selected_cost_bps"}, f"groups.{name}")
        symbols = group["symbols"]
        if (not isinstance(symbols, list) or not symbols
                or any(type(symbol) is not str or symbol not in universe for symbol in symbols)
                or len(set(symbols)) != len(symbols)):
            raise ValueError(f"groups.{name}.symbols must be unique supported symbols for that asset class")
        costs = group["costs_bps"]
        if not isinstance(costs, list) or not costs:
            raise ValueError(f"groups.{name}.costs_bps must be a nonempty list")
        numeric_costs = [_number(value, f"groups.{name}.costs_bps", 0, 1000) for value in costs]
        if numeric_costs != sorted(set(numeric_costs)):
            raise ValueError("costs_bps must be unique and ascending")
        selected = _number(group["selected_cost_bps"], f"groups.{name}.selected_cost_bps", 0, 1000)
        if selected not in numeric_costs:
            raise ValueError("selected_cost_bps must be one of the frozen stress scenarios")
    return json.loads(_canonical(result))


def load_protocol(path: Path) -> dict[str, Any]:
    with Path(path).open("rb") as handle:
        return validate_protocol(tomllib.load(handle))


def protocol_hash(protocol: dict[str, Any]) -> str:
    return _digest(validate_protocol(protocol))


def _safe_file(path: Path) -> Path:
    raw = Path(path)
    if ".." in raw.parts:
        raise ValueError("Snapshot paths must not contain parent-directory traversal")
    for candidate in (raw, raw.resolve()):
        for part in candidate.parts:
            lower = part.lower()
            if (lower.startswith(".env") or lower in {"credentials", "secrets", "id_rsa", "id_ed25519"}
                    or lower.endswith((".pem", ".key", ".p12", ".pfx"))):
                raise ValueError("Secret or credential paths cannot enter a research manifest")
    if raw.is_symlink():
        raise ValueError("Snapshot files must not be symbolic links")
    resolved = raw.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("Snapshot entries must be regular files")
    return resolved


def _file_records(paths: list[Path]) -> list[dict[str, object]]:
    if not isinstance(paths, list) or not paths:
        raise ValueError("Both input_files and code_files must be nonempty lists")
    resolved = [_safe_file(path) for path in paths]
    if len(set(resolved)) != len(resolved):
        raise ValueError("Snapshot paths must be unique")
    records = []
    for path in sorted(resolved):
        # Detect changes during hashing, including replacement of the file.
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
            after_read = os.fstat(handle.fileno())
        after = path.stat()
        signatures = {(item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
                      for item in (before, opened, after_read, after)}
        if len(signatures) != 1:
            raise ValueError("A snapshot file changed while it was being hashed")
        records.append({"path": str(path), "size_bytes": before.st_size, "sha256": digest.hexdigest()})
    return records


def _runtime() -> dict[str, object]:
    packages = {}
    for name in ("quant-paper-hft", "numpy", "pandas", "scikit-learn", "joblib", "exchange-calendars"):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = "not-installed"
    return {
        "python": platform.python_version(), "implementation": platform.python_implementation(),
        "platform": platform.system(), "machine": platform.machine(), "packages": packages,
    }


def _snapshot(protocol: dict[str, Any], input_files: list[Path], code_files: list[Path]) -> dict[str, Any]:
    clean = validate_protocol(protocol)
    return {
        "manifest_version": 1, "research_only": True, "executable_backtest": False,
        "promotion_disabled": True, "protocol": clean, "protocol_hash": protocol_hash(clean),
        "input_files": _file_records(input_files), "code_files": _file_records(code_files),
        "runtime": _runtime(),
    }


def _validate_manifest(manifest: object) -> dict[str, Any]:
    result = _shape(manifest, MANIFEST_KEYS, "manifest")
    without_digest = {key: value for key, value in result.items() if key != "manifest_hash"}
    if result["manifest_hash"] != _digest(without_digest):
        raise ValueError("Manifest integrity hash mismatch")
    stable = {key: value for key, value in result.items()
              if key not in {"created_at", "snapshot_hash", "manifest_hash"}}
    if result["snapshot_hash"] != _digest(stable):
        raise ValueError("Manifest snapshot hash mismatch")
    if (type(result["manifest_version"]) is not int or result["manifest_version"] != 1
            or result["research_only"] is not True or result["executable_backtest"] is not False
            or result["promotion_disabled"] is not True):
        raise ValueError("Manifest has an unsupported or executable research contract")
    if result["protocol_hash"] != protocol_hash(result["protocol"]):
        raise ValueError("Manifest protocol hash mismatch")
    try:
        created = datetime.fromisoformat(result["created_at"])
        if created.tzinfo is None or created.utcoffset().total_seconds() != 0:
            raise ValueError("Manifest creation time must be UTC")
    except (TypeError, ValueError):
        raise ValueError("Manifest creation time must be a valid UTC timestamp") from None
    return result


def verify_manifest(manifest: dict[str, Any], input_files: list[Path], code_files: list[Path]) -> None:
    """Raise ValueError if manifest integrity, file contents, or runtime changed."""
    clean = _validate_manifest(manifest)
    current = _snapshot(clean["protocol"], input_files, code_files)
    if _digest(current) != clean["snapshot_hash"]:
        raise ValueError("Research snapshot changed: configuration, data, code, paths, or runtime differ")


def _run_directory(run_dir: Path) -> Path:
    raw = Path(run_dir)
    if (".." in raw.parts or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", raw.name)
            or raw.is_symlink()):
        raise ValueError("run_dir must have a safe name and must not be a symlink or traversal")
    resolved = raw.resolve()
    if resolved in {Path(resolved.anchor), Path.home().resolve(), Path.cwd().resolve()}:
        raise ValueError("Use a dedicated research run directory")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError("run_dir must be a directory")
    return resolved


def freeze_manifest(
    run_dir: Path, protocol: dict[str, Any], input_files: list[Path], code_files: list[Path],
) -> dict[str, Any]:
    """Create manifest.json once; identical snapshots may reuse it without writes.

    The fully flushed temporary file is installed using a no-replace hard link.
    Concurrent writers cannot overwrite one another, and a crash cannot publish
    a partially serialized manifest. Inputs and runtime are never copied here.
    """
    directory = _run_directory(run_dir)
    snapshot = _snapshot(protocol, input_files, code_files)
    snapshot_hash = _digest(snapshot)
    destination = directory / "manifest.json"

    def existing_manifest() -> dict[str, Any]:
        if destination.is_symlink() or not destination.is_file():
            raise ValueError("Existing manifest must be a regular file")
        try:
            manifest = _validate_manifest(json.loads(destination.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            raise ValueError("Existing manifest is unreadable or incomplete") from None
        if manifest["snapshot_hash"] != snapshot_hash:
            raise ValueError("Run directory is frozen to a different research snapshot; choose a new directory")
        return manifest

    if destination.exists() or destination.is_symlink():
        return existing_manifest()
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("Unmanifested run directory must be empty")
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        **snapshot, "snapshot_hash": snapshot_hash,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    manifest["manifest_hash"] = _digest(manifest)
    descriptor, temporary = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical(manifest) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            return existing_manifest()
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return manifest
