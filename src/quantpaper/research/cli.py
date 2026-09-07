"""Offline-only, create-only v3 research runner, isolated from shadow and orders."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile

import pandas as pd

from ..ml.regime import CONTEXT_SYMBOLS
from ..scheduler import require_paper_gates_closed
from .evaluation import evaluate_group
from .protocol import freeze_manifest, load_protocol, verify_manifest
from .targets import build_timing_dataset


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _scoped_directory(run_dir: Path, project_root: Path) -> Path:
    root = project_root.resolve()
    raw = Path(os.path.abspath(run_dir))
    base = root / "artifacts" / "research-v3"
    if ".." in run_dir.parts or raw == base or not raw.is_relative_to(base):
        raise ValueError("run directory must be a dedicated child of artifacts/research-v3")
    # Reject every symlink inside project scope, not only an escaped final path.
    for node in (raw, *raw.parents):
        if node == root:
            break
        if node.is_symlink():
            raise ValueError("research output paths must not have symlink ancestors")
    if not raw.resolve().is_relative_to(base.resolve()):
        raise ValueError("research output resolves outside its isolated directory")
    return raw


def validate_run_directory(run_dir: Path, project_root: Path = PROJECT_ROOT) -> Path:
    directory = _scoped_directory(run_dir, project_root)
    if directory.exists():
        raise ValueError("run directory already exists; preserve evidence and use a new run name")
    return directory


def _create_bytes(path: Path, payload: bytes) -> None:
    """Publish fully flushed bytes without overwriting any existing artifact."""
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


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def _code_files(root: Path, protocol_path: Path) -> list[Path]:
    return sorted(set([
        *root.joinpath("src/quantpaper").rglob("*.py"), root / "main.py",
        root / "pyproject.toml", root / "requirements-tested.txt", protocol_path.resolve(),
    ]))


def run_research(
    protocol_path: Path, data_dir: Path, run_dir: Path, *, project_root: Path = PROJECT_ROOT,
) -> dict:
    directory = validate_run_directory(run_dir, project_root)
    require_paper_gates_closed(project_root / ".env")
    protocol = load_protocol(protocol_path)
    symbols = list(dict.fromkeys([
        *(symbol for group in protocol["groups"].values() for symbol in group["symbols"]),
        *CONTEXT_SYMBOLS,
    ]))
    paths = {symbol: data_dir / f"{symbol.replace('^', 'INDEX_')}.csv" for symbol in symbols}
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise ValueError(f"offline input caches missing: {missing}")
    inputs = list(paths.values())
    code = _code_files(project_root, protocol_path)
    # Exclusive mkdir provides race-safe run ownership. Never resume partial runs.
    directory.mkdir(parents=True, exist_ok=False)
    manifest = freeze_manifest(directory, protocol, inputs, code)
    cutoff = pd.Timestamp(protocol["historical_cutoff"], tz="UTC")
    frames = {}
    coverage = {}
    expected_hashes = {record["path"]: record["sha256"] for record in manifest["input_files"]}
    for symbol, path in paths.items():
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected_hashes[str(path.resolve())]:
            raise ValueError("an input changed after the research manifest was frozen")
        frame = pd.read_csv(io.BytesIO(payload), parse_dates=["timestamp"], index_col="timestamp")
        frame.index = pd.to_datetime(frame.index, utc=True)
        # Cut before feature construction; no post-cutoff prices can affect the fit.
        frame = frame.loc[frame.index.normalize() <= cutoff].copy()
        if frame.empty:
            raise ValueError(f"{symbol} has no cached history before the frozen cutoff")
        frames[symbol] = frame.sort_index()
        coverage[symbol] = {
            "rows": len(frame), "first_date": frame.index.min().date().isoformat(),
            "last_date": frame.index.max().date().isoformat(),
        }
    context = {symbol: frames[symbol] for symbol in CONTEXT_SYMBOLS}
    groups = {}
    predictions = []
    for group_name, config in protocol["groups"].items():
        dataset = pd.concat([
            build_timing_dataset(frames[symbol], symbol, context) for symbol in config["symbols"]
        ]).sort_index()
        print(f"Evaluating {group_name}: {len(dataset)} rows, frozen 3-model comparison", file=sys.stderr, flush=True)
        evaluation, output = evaluate_group(dataset, config, protocol)
        groups[group_name] = {"dataset_rows": len(dataset), **evaluation}
        output["group"] = group_name
        predictions.append(output)
    # Reject changed files/protocol/runtime rather than quietly publish mixed evidence.
    verify_manifest(manifest, inputs, code)
    report = {
        "protocol_id": protocol["protocol_id"], "protocol_hash": manifest["protocol_hash"],
        "snapshot_hash": manifest["snapshot_hash"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "research_only": True, "execution_enabled": False, "executable_backtest": False,
        "approved_for_paper_signals": False, "promotion_disabled": True,
        "deployment_artifact_created": False,
        "historical_cutoff": protocol["historical_cutoff"], "forward_start": protocol["forward_start"],
        "coverage": coverage, "groups": groups,
        "limitations": [
            "Daily adjusted Yahoo prices are research proxies, not verified executable fills or historical PIT vintages.",
            "BTC features are deliberately lagged two UTC dates; equity features use the exact previous XNYS session.",
            "Long/flat only, no short borrow or derivatives. Costs are frozen stress assumptions, not broker tariffs.",
            "Asset groups have separate models and may have different OOS windows; do not pool or directly rank their returns.",
            "Historical data were examined in previous research; this is not a pristine final holdout.",
            "Bootstrap intervals are descriptive and not adjusted for multiple model trials.",
            "Forward start is a reserved research boundary, not a claim that v3 predictions were recorded or trading started.",
            "The v2 model, its shadow journal, automatic schedule and order gates remain unchanged.",
        ],
    }
    prediction_bytes = pd.concat(predictions).sort_index().to_csv(index_label="timestamp").encode()
    report_bytes = _json_bytes(report)
    _create_bytes(directory / "predictions.csv", prediction_bytes)
    _create_bytes(directory / "report.json", report_bytes)
    _create_bytes(directory / "completion.json", _json_bytes({
        "status": "complete", "manifest_hash": manifest["manifest_hash"],
        "artifacts": {"predictions.csv": hashlib.sha256(prediction_bytes).hexdigest(),
                      "report.json": hashlib.sha256(report_bytes).hexdigest()},
    }))
    return report


def read_report(run_dir: Path, project_root: Path = PROJECT_ROOT) -> dict:
    # Validate the stored manifest itself without comparing today's source/data
    # files: historical evidence must remain readable after later research edits.
    from .protocol import _validate_manifest

    directory = _scoped_directory(run_dir, project_root)
    for name in ("completion.json", "manifest.json", "report.json", "predictions.csv"):
        if (directory / name).is_symlink():
            raise ValueError("research artifacts must not be symlinks")
    completion = json.loads((directory / "completion.json").read_text())
    manifest = _validate_manifest(json.loads((directory / "manifest.json").read_text()))
    if completion.get("status") != "complete" or completion.get("manifest_hash") != manifest.get("manifest_hash"):
        raise ValueError("incomplete run or manifest mismatch")
    if set(completion.get("artifacts", {})) != {"report.json", "predictions.csv"}:
        raise ValueError("unexpected research artifact list")
    for name, expected in completion["artifacts"].items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"research artifact integrity mismatch: {name}")
    return json.loads((directory / "report.json").read_text())


def summary(report: dict) -> dict:
    groups = {}
    for name, group in report["groups"].items():
        cost_key = f"{group['selected_cost_bps']:g}_bps"
        groups[name] = {
            "oos_start": group["oos_start"], "oos_end": group["oos_end"],
            "selected_round_trip_cost_bps": group["selected_cost_bps"],
            "models": {model: {
                "roc_auc": metrics["roc_auc"],
                **metrics["cost_stress"][cost_key],
            } for model, metrics in group["models"].items()},
        }
    return {"research_only": True, "execution_enabled": False,
            "approved_for_paper_signals": False, "groups": groups}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline research-v3; never submits orders or promotes models")
    parser.add_argument("command", choices=["run", "show"])
    parser.add_argument("--protocol", type=Path, default=PROJECT_ROOT / "configs/research_v3.toml")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data/yahoo")
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = (run_research(args.protocol, args.data_dir, args.run_dir)
                  if args.command == "run" else read_report(args.run_dir))
        print(json.dumps({"report_path": str(args.run_dir / "report.json"), **summary(report)}, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, RuntimeError) as error:
        print(f"Research error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
