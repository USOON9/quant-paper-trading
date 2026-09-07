"""Bounded quote pre-roll capture and reproducible offline as-of diagnostics."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

from ..scheduler import require_paper_gates_closed
from .client import MarketDataClient, MarketDataError
from .replay_book import analyze_replay_window
from .replay_protocol import build_replay_plan
from .storage import read_report, scoped_directory, validate_run_directory, write_json
from .study import source_snapshot as study_source_snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONTRACT = "intraday_asof_replay_v1"
REQUEST_INTERVAL_SECONDS = 0.4


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class Evidence:
    """Verify the closed artifact index, then recheck every file consumed."""

    def __init__(self, directory: Path, project_root: Path):
        self.project_root = project_root
        self.directory = scoped_directory(directory, project_root)
        self.report = read_report(self.directory, project_root)
        self.completion_raw = (self.directory / "completion.json").read_bytes()
        self.completion = json.loads(self.completion_raw)
        if len(self.completion["artifacts"]) > 1000:
            raise ValueError("source evidence exceeds the bounded artifact budget")
        # Pair the report returned above with the same index used below.
        if self.read("report.json") != self.report:
            raise ValueError("source evidence changed during verification")
        if self.report.get("plan_sha256") != self.completion["artifacts"]["plan.json"]:
            raise ValueError("source report is not paired with the indexed plan")
        indexed_source_digest = self.completion["artifacts"].get("source.json")
        plan = self.read("plan.json")
        if (not isinstance(indexed_source_digest, str)
                or self.report.get("source_snapshot_sha256") != indexed_source_digest
                or plan.get("source_snapshot_sha256") != indexed_source_digest):
            raise ValueError("source report is not paired with its source snapshot")
        if self.report.get("contract") == CONTRACT:
            if plan.get("source_study_plan_sha256") != self.completion["artifacts"].get("source-plan.json"):
                raise ValueError("source replay is not paired with its original study plan")

    def read(self, name: str) -> dict:
        if (Path(name).name != name or not name.endswith(".json")
                or name not in self.completion["artifacts"]):
            raise ValueError("required source file is not indexed")
        path = self.directory / name
        if path.is_symlink():
            raise ValueError("source evidence must not contain symbolic links")
        raw = path.read_bytes()
        if _digest(raw) != self.completion["artifacts"][name]:
            raise ValueError("source file integrity mismatch")
        return json.loads(raw)

    def unchanged(self) -> bool:
        try:
            if (self.directory / "completion.json").read_bytes() != self.completion_raw:
                return False
            return read_report(self.directory, self.project_root) == self.report
        except (ValueError, OSError, KeyError, TypeError):
            return False

    def identity(self) -> dict:
        return {"directory": str(self.directory), "contract": self.report["contract"],
                "completion_sha256": _digest(self.completion_raw),
                "report_sha256": self.completion["artifacts"]["report.json"],
                "plan_sha256": self.completion["artifacts"]["plan.json"]}


def source_snapshot() -> dict:
    snapshot = study_source_snapshot()
    for filename in ("replay_book.py", "replay_protocol.py", "replay_runner.py"):
        name = f"src/quantpaper/marketdata/{filename}"
        path = PROJECT_ROOT / name
        if not path.resolve().is_relative_to(PROJECT_ROOT.resolve()):
            raise ValueError("replay source snapshot cannot leave the project")
        for node in (path, *path.parents):
            if node == PROJECT_ROOT:
                break
            if node.is_symlink():
                raise ValueError("replay source snapshot cannot follow symbolic links")
        raw = path.read_bytes()
        snapshot["files"][name] = {"sha256": _digest(raw), "text": raw.decode("utf-8")}
    return snapshot


def _raw_name(window: dict) -> str:
    return f"{window['symbol'].replace('/', '-')}-{window['session']}-{window['window_name']}-quotes.json"


def _failed(window: dict, error: dict, *, attempted: bool) -> dict:
    return {
        "symbol": window["symbol"], "kind": "quotes", "feed": window["feed"],
        "start": window["start"], "end": window["end"], "observed_at": None,
        "failure_recorded_at": _stamp(), "attempted": attempted, "records": [], "pages": 0,
        "complete": False, "content_hash": _digest(b"[]"), "error": error,
        "truncation_reason": "request_failed" if attempted else "circuit_breaker_not_requested",
    }


def aggregate_replay(rows: list[dict]) -> dict:
    """Counts per symbol/window; eligibility is never a fill probability."""
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["symbol"], row["window_name"])].append(row)
    groups = []
    for (symbol, window), observations in sorted(grouped.items()):
        groups.append({
            "symbol": symbol, "window": window, "expected_samples": len(observations),
            "source_usable_samples": sum(row["source_usable"] for row in observations),
            "decision_states": dict(sorted(Counter(row["decision_state"]["status"] for row in observations).items())),
            "decision_reason_counts": dict(sorted(Counter(
                reason for row in observations for reason in set(row["decision_state"]["reasons"])).items())),
            "scenarios": [{
                "latency_ms": latency, "expected_samples": len(observations),
                "statuses": dict(sorted(Counter(
                    scenario["status"] for row in observations for scenario in row["scenarios"]
                    if scenario["latency_ms"] == latency).items())),
                "arrival_states": dict(sorted(Counter(
                    scenario["arrival_state"]["status"] for row in observations for scenario in row["scenarios"]
                    if scenario["latency_ms"] == latency).items())),
            } for latency in (0, 250, 1000)],
        })
    return {
        "expected_windows": len(rows), "expected_scenarios": len(rows) * 3,
        "decision_states": dict(sorted(Counter(row["decision_state"]["status"] for row in rows).items())),
        "scenario_statuses": dict(sorted(Counter(s["status"] for row in rows for s in row["scenarios"]).items())),
        "by_symbol_window": groups,
        "interpretation": "all planned windows retained; quote eligibility counts, NOT fill rates or strategy results",
    }


def run_replay(
    run_dir: Path, source_dir: Path, *, collect: bool = False,
    project_root: Path = PROJECT_ROOT, client=None, now: datetime | None = None,
) -> dict:
    """collect=True fetches short pre-rolls; False replays frozen raw quotes offline."""
    directory = validate_run_directory(run_dir, project_root)
    require_paper_gates_closed(project_root / ".env")
    if type(collect) is not bool or (not collect and client is not None):
        raise ValueError("offline replay does not accept a network client")
    evidence = Evidence(source_dir, project_root)
    input_plan = evidence.read("plan.json")
    if collect:
        if evidence.report.get("contract") != "intraday_multi_session_v1":
            raise ValueError("quote pre-roll collection requires a verified multi-session study")
        if (evidence.report.get("status") != "captured"
                or evidence.report.get("source_unchanged_during_capture") is not True):
            raise ValueError("source study must have completed without source changes")
        study_plan = input_plan
    else:
        if evidence.report.get("contract") != CONTRACT:
            raise ValueError("offline replay requires a saved as-of replay capture")
        if (evidence.report.get("status") not in {"captured", "replayed", "partial"}
                or evidence.report.get("source_unchanged_during_run") is not True
                or evidence.report.get("input_index_unchanged_during_run") is not True):
            raise ValueError("source replay has unverified source/integrity status")
        study_plan = evidence.read("source-plan.json")
    plan = build_replay_plan(study_plan, now=now if now is not None else datetime.now(timezone.utc))
    if not collect:
        for key in ("contract", "source_study", "windows", "policy", "limits", "summary"):
            if json.dumps(input_plan.get(key), sort_keys=True, allow_nan=False) != json.dumps(
                plan[key], sort_keys=True, allow_nan=False
            ):
                raise ValueError("frozen replay protocol does not match the verified study")
    snapshot = source_snapshot()
    plan["mode"] = "quote_preroll_capture" if collect else "offline_replay"
    plan["input_evidence"] = evidence.identity()
    plan["collection"] = {
        "http_scope": "GET data.alpaca.markets historical quotes only" if collect else "none; local files only",
        "min_http_request_interval_seconds": REQUEST_INTERVAL_SECONDS if collect else None,
        "on_error": "no retry or feed fallback; stop remaining requests and retain planned unavailable windows",
    }
    owned = collect and client is None
    if owned:
        client = MarketDataClient.from_env(project_root / ".env",
                                          min_request_interval_seconds=REQUEST_INTERVAL_SECONDS)
    first_http_count = getattr(client, "request_count", None) if collect else 0
    try:
        directory.mkdir(parents=True, exist_ok=False)
        artifacts = {"source.json": write_json(directory / "source.json", snapshot),
                     "source-plan.json": write_json(directory / "source-plan.json", study_plan)}
        plan["source_snapshot_sha256"] = artifacts["source.json"]
        plan["source_study_plan_sha256"] = artifacts["source-plan.json"]
        artifacts["plan.json"] = write_json(directory / "plan.json", plan)
        rows, errors = [], []
        circuit = None
        attempted = successful = skipped = raw_quotes = retained_pages = 0
        pagination_complete = True
        for window in plan["windows"]:
            filename = _raw_name(window)
            if not collect:
                payload = evidence.read(filename)
            elif circuit is not None:
                skipped += 1
                payload = _failed(window, {"category": "not_requested", "cause": circuit["category"]}, attempted=False)
            else:
                require_paper_gates_closed(project_root / ".env")
                attempted += 1
                try:
                    payload = client.fetch("quotes", window["symbol"], _parse(window["start"]),
                                           _parse(window["end"]), feed=window["feed"], max_pages=3)
                    successful += 1
                except MarketDataError as error:
                    circuit = {"symbol": window["symbol"], "session": window["session"],
                               "window": window["window_name"], "category": error.category, "message": str(error)}
                    errors.append(circuit)
                    payload = _failed(window, circuit, attempted=True)
            artifacts[filename] = write_json(directory / filename, payload)
            records = payload["records"]
            if payload.get("feed") != window["feed"]:
                raise ValueError("raw quote feed differs from the frozen replay plan")
            if payload.get("content_hash") != _digest(json.dumps(
                records, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()):
                raise ValueError("raw quote content hash mismatch")
            pagination_complete = pagination_complete and payload.get("complete") is True
            raw_quotes += len(records)
            retained_pages += payload.get("pages", 0)
            row = analyze_replay_window(window["symbol"], payload, target=_parse(window["target"]),
                                        max_age_ms=plan["policy"]["max_age_ms"],
                                        latencies_ms=tuple(plan["policy"]["latency_scenarios_ms"]))
            row.update(session=window["session"], window_name=window["window_name"], raw_file=filename)
            rows.append(row)
            if len(rows) % 15 == 0 or len(rows) == len(plan["windows"]):
                print(f"As-of replay: {len(rows)}/{len(plan['windows'])} windows analyzed", file=sys.stderr, flush=True)
        source_unchanged = source_snapshot() == snapshot
        input_unchanged = evidence.unchanged()
        last_http_count = getattr(client, "request_count", None) if collect else 0
        http_attempts = (last_http_count - first_http_count
                         if type(first_http_count) is int and type(last_http_count) is int else None)
        status = ("captured" if collect else "replayed") if pagination_complete and not errors else "partial"
        if not source_unchanged or not input_unchanged:
            status = "source_changed"
        report = {
            "contract": CONTRACT, "mode": plan["mode"], "status": status,
            "status_scope": "artifact/transport completion only; NOT a trading approval",
            "research_only": True, "execution_enabled": False, "model_updated": False,
            "executable_backtest": False, "fills_simulated": 0, "orders_submitted": 0,
            "started_at": plan["created_at"], "finished_at": _stamp(),
            "plan_sha256": artifacts["plan.json"], "source_snapshot_sha256": artifacts["source.json"],
            "source_unchanged_during_run": source_unchanged, "input_index_unchanged_during_run": input_unchanged,
            "input_evidence": evidence.identity(), "policy": plan["policy"],
            "counts": {"planned_windows": len(plan["windows"]), "attempted_request_segments": attempted,
                       "successful_request_segments": successful, "skipped_request_segments": skipped,
                       "http_attempts": http_attempts, "retained_source_pages": retained_pages,
                       "raw_quotes": raw_quotes},
            "errors": errors, "circuit_breaker": circuit,
            "aggregate": aggregate_replay(rows), "windows": rows,
            "limitations": [
                "Reconstructed vendor event-time quote state, not a full order book or verified historical receive-time state.",
                "Max quote age of 1000ms and five-second warmup are fixed research assumptions, not calibrated execution parameters.",
                "Strictly earlier events only; an update timestamp equal to the decision/arrival does not establish ordering.",
                "Decision rejection cannot be rescued by a future quote; eligible reference prices do not imply fills.",
                "No exchange halt/status stream, complete quote-condition rules, fees, liquidity capacity, queue or impact verification.",
                "No model, shadow database, account, orders, positions or automation is changed.",
            ],
        }
        artifacts["report.json"] = write_json(directory / "report.json", report)
        write_json(directory / "completion.json", {"status": "complete", "artifacts": artifacts})
        return report
    finally:
        if owned:
            client.close()


def replay_summary(report: dict) -> dict:
    return {key: report[key] for key in (
        "contract", "mode", "status", "status_scope", "execution_enabled", "model_updated",
        "fills_simulated", "orders_submitted", "source_unchanged_during_run", "input_index_unchanged_during_run",
        "counts", "errors", "circuit_breaker", "aggregate",
    )}
