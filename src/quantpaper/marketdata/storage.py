"""Isolated create-only files and verified historical capture reports."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile


def scoped_directory(run_dir: Path, project_root: Path) -> Path:
    root = project_root.resolve()
    raw = Path(os.path.abspath(run_dir))
    base = root / "artifacts/marketdata"
    if ".." in run_dir.parts or raw == base or not raw.is_relative_to(base):
        raise ValueError("capture directory must be a dedicated child of artifacts/marketdata")
    for node in (raw, *raw.parents):
        if node == root:
            break
        if node.is_symlink():
            raise ValueError("capture output must not use symbolic links")
    return raw


def validate_run_directory(run_dir: Path, project_root: Path) -> Path:
    directory = scoped_directory(run_dir, project_root)
    if directory.exists():
        raise ValueError("capture directory already exists; use a new name to preserve evidence")
    return directory


def write_json(path: Path, value: dict) -> str:
    payload = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return hashlib.sha256(payload).hexdigest()


def read_report(run_dir: Path, project_root: Path) -> dict:
    directory = scoped_directory(run_dir, project_root)
    completion_path = directory / "completion.json"
    if completion_path.is_symlink():
        raise ValueError("completion must not be a symbolic link")
    completion = json.loads(completion_path.read_text())
    artifacts = completion.get("artifacts")
    if completion.get("status") != "complete" or not isinstance(artifacts, dict):
        raise ValueError("capture has no completed evidence index")
    if "report.json" not in artifacts or "plan.json" not in artifacts:
        raise ValueError("capture is missing its plan/report")
    for name, digest in artifacts.items():
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(".json"):
            raise ValueError("unexpected artifact path in completion index")
        path = directory / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("capture artifact integrity mismatch")
    return json.loads((directory / "report.json").read_text())
